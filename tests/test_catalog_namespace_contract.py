"""Cross-work namespace boundary, using disposable real transfer ledgers."""

from tests._historical_replacement import historical_replacement
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from src.rename.work_catalog import WorkCatalog, WorkCatalogError
from tests import test_work_catalog as fixture


class CatalogNamespaceContracts(unittest.TestCase):
    def root(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name) / 'namespace'
        root.mkdir()
        return root

    def plans(self, root, *, same_root):
        first = fixture.movie_plan(root, 'work-12345678-1234-1234-1234-123456789abc')
        second = fixture.movie_plan(root, 'work-22345678-1234-1234-1234-123456789abc',
                                    target_name='Example (2022) - 2160p.mkv')
        target = second.entries[0].target_path
        if not same_root:
            target = target.parent.with_name('Other Example (2022)') / target.name
        second = replace(second, task_id='task-two',
            work_identity=replace(second.work_identity, primary_provider=fixture.provider('997318')),
            entries=(replace(second.entries[0], source_path=root / 'second.mkv', target_path=target),),
            metadata={**second.metadata, 'canonical_work_root': str(target.parent)})
        # Both reviewed components exist before either work is registered.
        return fixture.trusted(first), fixture.trusted(second)

    def test_different_work_keys_cannot_register_same_root_with_distinct_leaf_names(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'namespace'
            root.mkdir()
            catalog = WorkCatalog(root / 'catalog')
            first, second = self.plans(root, same_root=True)
            first_ledger = fixture.complete_ledger(first)
            second_ledger = fixture.complete_ledger(second)
            self.assertNotEqual(first.entries[0].target_path, second.entries[0].target_path)
            self.assertTrue(all(entry.target_path.exists() for plan in (first, second) for entry in plan.entries))
            registered = catalog.register_completed(first, first_ledger)
            first_path = catalog._path(registered['work_key'])
            original = first_path.read_bytes()
            with self.assertRaisesRegex(WorkCatalogError, '目录|规范根|作品冲突'):
                catalog.register_completed(second, second_ledger)
            self.assertEqual(original, first_path.read_bytes())
            self.assertFalse(catalog._path(second.metadata['proposed_work_key']).exists())

    def test_different_works_with_distinct_roots_remain_legal(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'namespace'
            root.mkdir()
            catalog = WorkCatalog(root / 'catalog')
            first, second = self.plans(root, same_root=False)
            records = [catalog.register_completed(plan, fixture.complete_ledger(plan)) for plan in (first, second)]
            self.assertNotEqual(records[0]['canonical_root'], records[1]['canonical_root'])
            self.assertEqual(2, len(tuple(catalog.root.glob('work-*.json'))))

    def test_claimed_or_expired_lock_is_not_authority(self):
        for expired in (False, True):
            with self.subTest(expired=expired):
                root = self.root()
                catalog = WorkCatalog(root / 'catalog')
                plan, _ = self.plans(root, same_root=False)
                ledger = fixture.complete_ledger(plan)
                key = plan.metadata['proposed_work_key']
                if expired:
                    with catalog.lock(key):
                        pass
                with self.assertRaisesRegex(WorkCatalogError, '锁|持有|租约'):
                    catalog.register_completed(plan, ledger, lock_held=True)
                self.assertFalse(catalog._path(key).exists())

    def test_lease_wrong_key_and_wrong_root_cannot_write(self):
        root = self.root()
        catalog = WorkCatalog(root / 'catalog')
        other = WorkCatalog(root / 'other-catalog')
        first, second = self.plans(root, same_root=False)
        first_ledger, second_ledger = fixture.complete_ledger(first), fixture.complete_ledger(second)
        with catalog.lock(first.metadata['proposed_work_key']):
            for target, plan, ledger in ((catalog, second, second_ledger), (other, first, first_ledger)):
                with self.subTest(root=target.root.name, task=plan.task_id):
                    with self.assertRaisesRegex(WorkCatalogError, '锁|持有|租约'):
                        target.register_completed(plan, ledger, lock_held=True)
                    self.assertFalse(target._path(plan.metadata['proposed_work_key']).exists())

    def test_other_thread_and_other_process_cannot_claim_held_lease(self):
        root = self.root()
        catalog = WorkCatalog(root / 'catalog')
        plan, _ = self.plans(root, same_root=False)
        ledger = fixture.complete_ledger(plan)
        key = plan.metadata['proposed_work_key']
        with catalog.lock(key):
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(catalog.register_completed, plan, ledger, lock_held=True)
                with self.assertRaisesRegex(WorkCatalogError, '锁|持有|租约'):
                    future.result(timeout=5)
            command = (
                'import json,sys; from pathlib import Path; '
                'from src.rename.work_catalog import WorkCatalog,WorkCatalogError; '
                'from src.rename.plan_store import PlanStore; '
                'plan=PlanStore(Path(sys.argv[1])).load(sys.argv[2]); '
                'catalog=WorkCatalog(Path(sys.argv[3])); ledger=json.loads(Path(sys.argv[4]).read_text())\n'
                'try: catalog.register_completed(plan,ledger,lock_held=True)\n'
                "except WorkCatalogError: print('rejected')\n"
                "else: print('accepted')\n")
            result = subprocess.run([sys.executable, '-B', '-X', 'utf8', '-c', command,
                str(root / 'plans'), plan.task_id, str(catalog.root),
                str(root / 'records' / f'{plan.task_id}.ledger.json')],
                text=True, encoding='utf-8', capture_output=True, timeout=10,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual('rejected', result.stdout.strip())
        self.assertFalse(catalog._path(key).exists())

    def test_snapshot_detects_set_and_same_revision_byte_drift(self):
        root = self.root()
        catalog = WorkCatalog(root / 'catalog')
        first, second = self.plans(root, same_root=False)
        record = catalog.register_completed(first, fixture.complete_ledger(first))
        key = record['work_key']
        method = getattr(catalog, 'namespace_snapshot', None)
        self.assertTrue(callable(method), 'missing strict namespace snapshot')
        with catalog.lock(key):
            snapshot = method(key)
            path = catalog._path(key)
            original = path.read_bytes()
            path.write_bytes(original + b'\n')
            with self.assertRaisesRegex(WorkCatalogError, '变化|快照'):
                catalog.verify_namespace_snapshot(key, snapshot)
            path.write_bytes(original)
            catalog.verify_namespace_snapshot(key, snapshot)
        catalog.register_completed(second, fixture.complete_ledger(second))
        with catalog.lock(key):
            with self.assertRaisesRegex(WorkCatalogError, '变化|快照'):
                catalog.verify_namespace_snapshot(key, snapshot)
            current = method(key)
            catalog._path(second.metadata['proposed_work_key']).unlink()
            with self.assertRaisesRegex(WorkCatalogError, '变化|快照'):
                catalog.verify_namespace_snapshot(key, current)

    def test_strict_snapshot_rejects_corrupt_or_forged_record_set(self):
        root = self.root()
        catalog = WorkCatalog(root / 'catalog')
        first, _ = self.plans(root, same_root=False)
        record = catalog.register_completed(first, fixture.complete_ledger(first))
        key = record['work_key']
        method = getattr(catalog, 'namespace_snapshot', None)
        self.assertTrue(callable(method), 'missing strict namespace snapshot')
        with catalog.lock(key):
            snapshot = method(key)
            with self.assertRaisesRegex(WorkCatalogError, '快照|类型'):
                catalog.verify_namespace_snapshot(key, {'fingerprint': snapshot.fingerprint})
            (catalog.root / 'work-32345678-1234-1234-1234-123456789abc.json').write_text('{', encoding='utf-8')
            with self.assertRaisesRegex(WorkCatalogError, '损坏|读取|格式|完整'):
                method(key)

    def test_different_keys_compete_for_same_namespace(self):
        root = self.root()
        catalog = WorkCatalog(root / 'catalog')
        other = WorkCatalog(catalog.root)
        first, second = self.plans(root, same_root=False)
        def contender():
            with other.lock(second.metadata['proposed_work_key']):
                return 'accepted'
        with catalog.lock(first.metadata['proposed_work_key']):
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(contender)
                with self.assertRaisesRegex(WorkCatalogError, '锁|占用|任务|登记'):
                    future.result(timeout=5)
        self.assertFalse(tuple(catalog.root.glob('work-*.json')))

    def test_windows_root_aliases_and_conflicting_provider_are_rejected(self):
        for collision in ('root_alias', 'provider'):
            with self.subTest(collision=collision):
                root = self.root()
                catalog = WorkCatalog(root / 'catalog')
                first, second = self.plans(root, same_root=False)
                saved = catalog.register_completed(first, fixture.complete_ledger(first))
                candidate = {**saved, 'work_key': second.metadata['proposed_work_key'],
                    'canonical_root': str(second.entries[0].target_path.parent)}
                if collision == 'root_alias':
                    candidate['providers'] = [second.work_identity.primary_provider.to_dict()]
                    candidate['canonical_root'] = str(first.entries[0].target_path.parent).upper().replace('\\', '/')
                method = getattr(catalog, 'validate_namespace_candidate', None)
                self.assertTrue(callable(method), 'missing shared prospective namespace validator')
                with catalog.lock(candidate['work_key']):
                    with self.assertRaisesRegex(WorkCatalogError, '目录|条目|冲突'):
                        method(candidate['work_key'], candidate)

    def test_register_relocate_archive_reject_disconnected_shared_guard(self):
        for action in ('register', 'relocate', 'archive'):
            for response in (None, object()):
                with self.subTest(action=action, response=type(response).__name__):
                    root = self.root()
                    catalog = WorkCatalog(root / 'catalog')
                    first, _ = self.plans(root, same_root=False)
                    ledger = fixture.complete_ledger(first)
                    key = first.metadata['proposed_work_key']
                    tasks = root / 'tasks'
                    tasks.mkdir()
                    if action != 'register':
                        catalog.register_completed(first, ledger)
                    if action == 'relocate':
                        new_root = root / 'relocated'
                        first.entries[0].target_path.parent.rename(new_root)
                    if action == 'archive':
                        fixture.remove_fixture_materialization(first)
                    before = {p.name: p.read_bytes() for p in catalog.root.glob('work-*.json')}
                    with patch.object(catalog, 'validate_namespace_candidate', return_value=response, create=True):
                        with self.assertRaisesRegex(WorkCatalogError, '验证|命名空间|候选'):
                            if action == 'register':
                                catalog.register_completed(first, ledger)
                            elif action == 'relocate':
                                catalog.relocate(key, new_root, allowed_root=root, task_root=tasks)
                            else:
                                catalog.archive_missing(key, task_root=tasks)
                    self.assertEqual(before, {p.name: p.read_bytes() for p in catalog.root.glob('work-*.json')})

    def test_relocate_and_archive_can_repair_existing_root_collision(self):
        for action in ('relocate', 'archive'):
            with self.subTest(action=action):
                root = self.root()
                catalog = WorkCatalog(root / 'catalog')
                first, second = self.plans(root, same_root=False)
                record = catalog.register_completed(first, fixture.complete_ledger(first))
                # Historical duplicate fixture, not a legitimate writer bypass.
                duplicate = {**record, 'work_key': second.metadata['proposed_work_key'],
                    'providers': [second.work_identity.primary_provider.to_dict()]}
                catalog._path(duplicate['work_key']).write_text(json.dumps(duplicate), encoding='utf-8')
                tasks = root / 'tasks'
                tasks.mkdir()
                if action == 'relocate':
                    new_root = root / 'relocated'
                    first.entries[0].target_path.parent.rename(new_root)
                    saved = catalog.relocate(record['work_key'], new_root, allowed_root=root, task_root=tasks)
                    self.assertEqual(str(new_root), saved['canonical_root'])
                else:
                    fixture.remove_fixture_materialization(first)
                    archived = catalog.archive_missing(record['work_key'], task_root=tasks)
                    self.assertTrue(archived.is_file())
                self.assertEqual(duplicate, catalog.load(duplicate['work_key']))

    def test_validation_cannot_be_replayed_forged_or_used_after_record_drift(self):
        from src.rename.catalog_namespace import consume
        root = self.root()
        catalog = WorkCatalog(root / 'catalog')
        first, _ = self.plans(root, same_root=False)
        record = catalog.register_completed(first, fixture.complete_ledger(first))
        key = record['work_key']
        with catalog.lock(key) as lease:
            with self.assertRaises(AttributeError):
                lease.key = 'work-92345678-1234-1234-1234-123456789abc'
            proof = catalog.validate_namespace_candidate(key, record)
            with self.assertRaisesRegex(WorkCatalogError, '验证|失效'):
                consume(catalog, key, record, replace(proof))
            consume(catalog, key, record, proof)
            with self.assertRaisesRegex(WorkCatalogError, '验证|失效'):
                consume(catalog, key, record, proof)
            proof = catalog.validate_namespace_candidate(key, record)
            path = catalog._path(key)
            path.write_bytes(path.read_bytes() + b'\n')
            with self.assertRaisesRegex(WorkCatalogError, '快照|变化'):
                consume(catalog, key, record, proof)


    def test_ordinary_namespace_requires_its_exact_live_issued_instance(self):
        from src.rename import catalog_namespace as ns
        from src.rename.work_catalog import WorkCatalog
        from src.rename.series_catalog import SeriesCatalog
        from pathlib import Path
        import tempfile
        for factory, prefix in ((WorkCatalog, 'work'), (SeriesCatalog, 'series')):
            with self.subTest(backend=prefix), tempfile.TemporaryDirectory() as temp:
                catalog = factory(Path(temp) / 'catalog')
                key = prefix + '-12345678-1234-1234-1234-123456789abc'
                with catalog.lock(key) as original:
                    self.assertIs(original, ns.require_lease(catalog, key))
                    with patch.object(ns._held, 'lease', replace(original)):
                        with self.assertRaises((ValueError, RuntimeError)):
                            ns.require_lease(catalog, key)
                    self.assertIs(original, ns.require_lease(catalog, key))
                    for field, changed in (('key', 'work-other'), ('issued', {})):
                        previous = getattr(original, field)
                        with self.subTest(binding=field):
                            try:
                                object.__setattr__(original, field, changed)
                                with self.assertRaises((ValueError, RuntimeError)):
                                    ns.require_lease(catalog, key)
                            finally:
                                object.__setattr__(original, field, previous)
                    life = ns._live_namespace_leases.pop(id(original))
                    try:
                        with self.assertRaises((ValueError, RuntimeError)):
                            ns.require_lease(catalog, key)
                    finally:
                        ns._live_namespace_leases[id(original)] = life
                for stale in (original, replace(original)):
                    with self.subTest(stale_is_original=stale is original), patch.object(ns._held, 'lease', stale):
                        with self.assertRaises((ValueError, RuntimeError)):
                            ns.require_lease(catalog, key)
                with catalog.lock(key) as current:
                    self.assertIs(current, ns.require_lease(catalog, key))
                    with patch.object(ns._held, 'lease', original):
                        with self.assertRaises((ValueError, RuntimeError)):
                            ns.require_lease(catalog, key)
                self.assertFalse(ns._live_namespace_leases)
                with self.assertRaisesRegex(RuntimeError, 'owned body fault'):
                    with catalog.lock(key):
                        raise RuntimeError('owned body fault')
                self.assertFalse(ns._live_namespace_leases)
                self.assertIsNone(ns._held.lease)


class CatalogNamespaceMediaContracts(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from tests.test_asset_execution_recovery_contract import AssetExecutionRecoveryContracts
        self.h = AssetExecutionRecoveryContracts('runTest')
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)

    def assert_legacy(self, plan, service):
        self.assertNotIn('content_policy', plan.metadata)
        self.assertIsNone(service.decision_state_store.load(plan.task_id).content_policy)
        task = service._read_task(plan.task_id)
        self.assertEqual(1, task['task_source_scope']['schema_version'])
        self.assertNotIn('content_policy', task['task_source_scope'])

    def duplicate(self, catalog, plan):
        key = plan.metadata.get('catalog_work_key') or plan.metadata['proposed_work_key']
        record = catalog.load(key)
        other = 'work-92345678-1234-1234-1234-123456789abc'
        record.update(work_key=other, providers=[fixture.provider('991234').to_dict()], work_intent=None)
        path = catalog._path(other)
        path.write_text(json.dumps(record), encoding='utf-8')
        return path

    async def test_execution_and_resume_block_conflict_before_transfer(self):
        from src.rename.plan_store import PlanStore
        from src.rename.asset_resume import resume_asset
        from src.rename.trans import Trans
        from tests.test_content_policy_contract import historical_admission
        with historical_admission():
            plan, service, transfer = await self.h.approved()
        self.assert_legacy(plan, service)
        fp = PlanStore.fingerprint(plan)
        duplicate = self.duplicate(service.work_catalog, plan)
        before = {entry.target_path: entry.target_path.read_bytes() for entry in plan.entries if entry.target_path.exists()}
        with patch('src.rename.plan_execution.is_dry_run_enabled', return_value=False), patch.object(Trans, 'trans_file', side_effect=AssertionError('unexpected media')) as media:
            with self.assertRaisesRegex((ValueError, RuntimeError), '目录|冲突'):
                service.execute(plan.task_id, fp)
            media.assert_not_called()
        self.assertEqual(before, {path: path.read_bytes() for path in before})
        duplicate.unlink()  # Only the injected disposable bad record.
        self.h.interrupt_after_publish(transfer)
        self.assertEqual('resume_media', self.h.recovery(service).inspect(plan.task_id, fp).state)
        duplicate = self.duplicate(service.work_catalog, plan)
        before = {entry.target_path: entry.target_path.read_bytes() for entry in plan.entries if entry.target_path.exists()}
        with patch('src.rename.plan_execution.is_dry_run_enabled', return_value=False), patch.object(Trans, 'trans_file', side_effect=AssertionError('unexpected media')) as media:
            with self.assertRaisesRegex((ValueError, RuntimeError), '目录|冲突'):
                resume_asset(service, plan.task_id, fp)
            media.assert_not_called()
        self.assertEqual(before, {path: path.read_bytes() for path in before})

    @historical_replacement
    async def test_rollback_checks_namespace_before_inverse_and_can_resume(self):
        from src.rename.plan_store import PlanStore
        from src.rename.trans import Trans
        from src.rename import asset_rollback
        from tests.test_content_policy_contract import historical_admission
        with historical_admission():
            plan, service, _transfer = await self.h.approved(omit_companion=True)
        self.assert_legacy(plan, service)
        fp = PlanStore.fingerprint(plan)
        with patch('src.rename.plan_execution.is_dry_run_enabled', return_value=False):
            service.execute(plan.task_id, fp)
        prepared = service.prepare_asset_rollback(plan.task_id, fp)
        duplicate = self.duplicate(service.work_catalog, plan)
        before = {entry.target_path: entry.target_path.read_bytes() for entry in plan.entries if entry.target_path.exists()}
        with patch('src.rename.plan_execution.is_dry_run_enabled', return_value=False), patch.object(Trans, 'recover_replacements', side_effect=AssertionError('unexpected inverse')) as media:
            with self.assertRaisesRegex((ValueError, RuntimeError), '目录|冲突'):
                asset_rollback.rollback(service, plan.task_id, fp, expected_recovery_fingerprint=prepared.fingerprint)
            media.assert_not_called()
        self.assertEqual(before, {path: path.read_bytes() for path in before})
        duplicate.unlink()
        # Final writer must recheck after the inverse; then resume media_restored.
        original = service.work_catalog.validate_namespace_candidate
        count = []
        def disconnect_final(key, candidate):
            count.append(key)
            return None if len(count) == 2 else original(key, candidate)
        key = plan.metadata.get('catalog_work_key') or plan.metadata['proposed_work_key']
        catalog_before = service.work_catalog._path(key).read_bytes()
        with patch('src.rename.plan_execution.is_dry_run_enabled', return_value=False), patch.object(service.work_catalog, 'validate_namespace_candidate', side_effect=disconnect_final):
            with self.assertRaisesRegex(RuntimeError, '候选验证'):
                asset_rollback.rollback(service, plan.task_id, fp, expected_recovery_fingerprint=prepared.fingerprint)
        self.assertEqual(2, len(count))
        self.assertEqual(catalog_before, service.work_catalog._path(key).read_bytes())
        with patch('src.rename.plan_execution.is_dry_run_enabled', return_value=False):
            result = asset_rollback.rollback(service, plan.task_id, fp, expected_recovery_fingerprint=prepared.fingerprint)
        self.assertEqual('rolled_back', result.ledger_status)

    @historical_replacement
    async def test_shared_guard_disconnect_stops_execute_resume_and_inverse(self):
        from tests.test_content_policy_contract import historical_admission
        from src.rename.plan_store import PlanStore
        from src.rename.trans import Trans
        from src.rename.asset_resume import resume_asset
        from src.rename import asset_rollback
        with historical_admission():
            plan, service, transfer = await self.h.approved(omit_companion=True)
        self.assert_legacy(plan, service)
        fp = PlanStore.fingerprint(plan)
        with patch('src.rename.plan_execution.is_dry_run_enabled', return_value=False), patch.object(service.work_catalog, 'validate_namespace_candidate', return_value=None), patch.object(Trans, 'trans_file') as media:
            with self.assertRaisesRegex((ValueError, RuntimeError), '候选验证'):
                service.execute(plan.task_id, fp)
            media.assert_not_called()
        self.h.interrupt_after_publish(transfer)
        with patch('src.rename.plan_execution.is_dry_run_enabled', return_value=False), patch.object(service.work_catalog, 'validate_namespace_candidate', return_value=None), patch.object(Trans, 'trans_file') as media:
            with self.assertRaisesRegex((ValueError, RuntimeError), '候选验证'):
                resume_asset(service, plan.task_id, fp)
            media.assert_not_called()
        with patch('src.rename.plan_execution.is_dry_run_enabled', return_value=False):
            result = resume_asset(service, plan.task_id, fp)
        self.assertEqual('completed', result.ledger_status)
        prepared = service.prepare_asset_rollback(plan.task_id, fp)
        with patch('src.rename.plan_execution.is_dry_run_enabled', return_value=False), patch.object(service.work_catalog, 'validate_namespace_candidate', return_value=None), patch.object(Trans, 'recover_replacements') as media:
            with self.assertRaisesRegex((ValueError, RuntimeError), '候选验证'):
                asset_rollback.rollback(service, plan.task_id, fp, expected_recovery_fingerprint=prepared.fingerprint)
            media.assert_not_called()


if __name__ == '__main__':
    unittest.main()
