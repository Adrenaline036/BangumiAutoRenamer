"""Incident-shaped contracts, disposable source/media only."""
import ast
import hashlib
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from src.rename.plan_store import PlanStore
from tests import test_movie_direct_replacement as movie_fixture
from tests import test_asset_execution_recovery_contract as recovery_fixture
from tests import test_whole_season_integration as tv_fixture


class MovieSelectionIncident(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.h = movie_fixture.MovieDirectReplacement('runTest')
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)

    async def pending(self):
        old, initial, service, primary, record = await self.h.incoming(resolution='2160p')
        version = record['versions'][0]
        intent = service.prepare_initial_movie_completion(initial.task_id, PlanStore.fingerprint(initial),
            groups=[dict(sources=[str(primary)], label=version['display_label'])],
            replace_version_ref=version['version_ref'], reason='先选择换源，随后改为新增')
        return service, service.commit_initial_movie_completion(intent), primary, version

    async def test_replacement_to_duplicate_append_rejects_without_writes(self):
        from tests.test_content_choice_contract import records
        service, plan, primary, version = await self.pending()
        before = records(self.h.h.root)
        with self.assertRaisesRegex(ValueError, '相同版本标签'):
            service.revise_movie_versions(plan.task_id, PlanStore.fingerprint(plan),
                groups=[dict(sources=[str(primary)], label=version['display_label'])],
                excluded_sources=[], replace_version_ref=None)
        self.assertEqual(before, records(self.h.h.root))

    async def test_append_uses_new_ref_and_specifications_then_normal_edit_is_stable(self):
        service, plan, primary, version = await self.pending()
        changed = service.revise_movie_versions(plan.task_id, PlanStore.fingerprint(plan),
            groups=[dict(sources=[str(primary)], label='新 2160p')], excluded_sources=[], replace_version_ref=None)
        group = changed.metadata['movie_version_selection']['groups'][0]
        self.assertNotEqual(version['version_ref'], group['version_ref'])
        self.assertIsNone(changed.metadata['movie_version_selection']['replacement'])
        self.assertEqual('2160p', group['descriptor']['resolution']['value'])
        edited = service.revise_movie_versions(plan.task_id, PlanStore.fingerprint(changed),
            groups=[dict(sources=[str(primary)], label='新版本改名')], excluded_sources=[])
        self.assertEqual(group['version_ref'], edited.metadata['movie_version_selection']['groups'][0]['version_ref'])


class ReplacementCompletionIncident(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.h = recovery_fixture.AssetExecutionRecoveryContracts('runTest')
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        from functools import partial
        self.h.fixture.incoming = partial(self.h.fixture.incoming, valid_media=True)

    async def prepared(self):
        return await self.h.approved(omit_companion=True)

    def execute(self, service, plan):
        with patch('src.rename.plan_execution.is_dry_run_enabled', return_value=False):
            return service.execute(plan.task_id, PlanStore.fingerprint(plan))

    def resume(self, service, plan):
        from src.rename.asset_resume import resume_asset
        with patch('src.rename.plan_execution.is_dry_run_enabled', return_value=False):
            return resume_asset(service, plan.task_id, PlanStore.fingerprint(plan))

    def assert_finished(self, plan, service, transfer):
        directory = transfer.replacement_authority.transaction_directory
        self.assertEqual({'journal.json'}, {p.name for p in directory.iterdir()})
        journal = json.loads((directory/'journal.json').read_text('utf-8'))
        self.assertEqual('cleanup_completed', journal['cleanup']['status'])
        for entry in plan.entries:
            if entry.collision_resolution is not None:
                # An explicitly reused identical companion retains its existing inode.
                from src.rename.asset_collision import validate_reuse_resolution
                validate_reuse_resolution(entry.source_path, entry.target_path, entry.collision_resolution)
            else:
                self.assertTrue(os.path.samefile(entry.source_path, entry.target_path), str(entry.source_path))
        self.assertEqual('completed', self.h.recovery(service).inspect(plan.task_id, PlanStore.fingerprint(plan)).state)
        task = service._read_task(plan.task_id)
        self.assertEqual('completed', task['workflow_status'])

    async def test_completed_movie_removes_temporary_links_and_rejects_inverse(self):
        plan, service, transfer = await self.prepared()
        self.execute(service, plan)
        self.assert_finished(plan, service, transfer)
        from src.pages.data_table_page import can_offer_asset_rollback
        self.assertFalse(can_offer_asset_rollback(service._read_task(plan.task_id), service.decision_state_store.load(plan.task_id)))
        with self.assertRaises(ValueError):
            service.prepare_asset_rollback(plan.task_id, PlanStore.fingerprint(plan))
        with self.assertRaises(ValueError):
            transfer.recover_replacements(rollback=True)

    async def test_publish_error_keeps_forward_progress_then_resumes(self):
        plan, service, transfer = await self.prepared()
        original = os.replace
        selected = Path(transfer.replacement_authority.evidence[0].target)
        def fail_after_publish(source, target):
            original(source, target)
            if Path(target) == selected:
                raise OSError('controlled interruption after actual publish')
        with patch('src.rename.trans.os.replace', side_effect=fail_after_publish):
            self.assertIsNotNone(transfer.trans_file())
        self.assertTrue(os.path.samefile(selected, next(e.source_path for e in plan.entries if e.target_path == selected)))
        self.resume(service, plan)
        self.assert_finished(plan, service, transfer)

    async def test_cleanup_interruption_resumes_without_reexecuting_media(self):
        plan, service, transfer = await self.prepared()
        directory = transfer.replacement_authority.transaction_directory
        original = Path.unlink
        count = []
        def interrupt(path, *args, **kwargs):
            ledger = service.record_root / (plan.task_id + '.ledger.json')
            if (path.parent == directory and path.name.startswith(('old-', 'retired-'))
                    and ledger.exists() and json.loads(ledger.read_text('utf-8'))['status'] == 'completed'):
                count.append(path.name)
                if len(count) == 2:
                    raise OSError('cleanup interrupted')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'unlink', interrupt):
            try:
                self.execute(service, plan)
            except (ValueError, RuntimeError, OSError):
                pass
        self.assertGreaterEqual(len(count), 2, '必须真的进入完成后的逐项清理')
        self.assertNotEqual('completed', service._read_task(plan.task_id)['workflow_status'])
        with patch('src.rename.trans.Trans.trans_file', side_effect=AssertionError('不得重放媒体')):
            self.resume(service, plan)
        self.assert_finished(plan, service, transfer)

    async def test_foreign_transaction_file_blocks_cleanup_without_deleting_it(self):
        plan, service, transfer = await self.prepared()
        self.assertIsNone(transfer.trans_file())
        directory = transfer.replacement_authority.transaction_directory
        foreign = directory/'user-note.txt'
        foreign.write_bytes(b'not owned')
        try:
            self.resume(service, plan)
        except (ValueError, RuntimeError, OSError):
            pass
        self.assertEqual(b'not owned', foreign.read_bytes())
        self.assertNotEqual('completed', service._read_task(plan.task_id)['workflow_status'])
        self.assertTrue(list(directory.glob('old-*')))
        foreign.unlink()  # Test-owned unknown file removed before authorized retry.
        self.resume(service, plan)
        self.assert_finished(plan, service, transfer)


class SeasonCompletionIncident(unittest.IsolatedAsyncioTestCase):
    async def test_tv_completion_removes_only_this_transaction_links(self):
        h = tv_fixture.WholeSeasonIntegration('runTest')
        h.setUp()
        self.addCleanup(h.doCleanups)
        result, target, extra, other = await h.prepare_old()
        task, ledger, service = await h.h._approve_and_execute(root=h.root, result=result)
        self.assertEqual('completed', task['execution_status'], task.get('error'))
        from src.rename.asset_replacement import approved_batch
        directory = approved_batch(result.plan, PlanStore.fingerprint(result.plan)).transaction_directory
        self.assertEqual({'journal.json'}, {p.name for p in directory.iterdir()})
        self.assertEqual(b'other', other.read_bytes())
        self.assertFalse(extra.exists())


class ProgressInventory(unittest.TestCase):
    def test_no_parallel_progress_builders_or_unapproved_spinners_remain(self):
        root = Path(__file__).resolve().parents[1]/'src'
        found = []
        for path in root.rglob('*.py'):
            tree = ast.parse(path.read_text('utf-8-sig'))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    if node.func.attr == 'linear_progress' and path.name not in {'progress_view.py', 'data_table_page.py'}:
                        found.append((path.name, node.lineno, 'linear_progress'))
                    if (node.func.attr == 'props' and node.args
                            and isinstance(node.args[0], ast.Constant)
                            and isinstance(node.args[0].value, str)
                            and 'loading' in node.args[0].value.split()):
                        found.append((path.name, node.lineno, 'button loading spinner'))
                    if node.func.attr == 'circular_progress':
                        found.append((path.name, node.lineno, 'circular_progress'))
                    if node.func.attr == 'spinner':
                        line = path.read_text('utf-8-sig').splitlines()[node.lineno-1]
                        if 'bar-font-check-ring' not in line:
                            found.append((path.name, node.lineno, 'spinner'))
        self.assertEqual([], found)


class LegacyDraftLabels(unittest.TestCase):
    def test_draft_producer_rejects_duplicate_before_persisting(self):
        import tempfile
        from tests import test_isolated_sample_acceptance as f
        from tests.test_work_catalog import complete_ledger
        from tests.test_content_choice_contract import records
        from src.rename.work_catalog import WorkCatalog
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            h = f.IsolatedSampleAcceptanceTests('runTest')
            source, allowed, draft, store, service = h._inventory_plan_with_identity(
                root, ('Example Work [1080p].mkv',), task_id='old-version')
            snapshot = f.routing_snapshot_for(allowed/'AnimeMovies', domain='animation', group_kind='movie_continuous')
            first = service.complete_inventory_draft(draft.task_id, store.fingerprint(draft),
                group_id=draft.presentation_groups[0].group_id, group_kind='movie_continuous',
                target_root=allowed/'AnimeMovies', reason='登记旧版本', root_snapshot=snapshot,
                movie_version_groups=[dict(sources=[str(next(source.glob('*.mkv')))], label='1080p')])
            WorkCatalog(root/'catalog').register_completed(first, complete_ledger(first, root/'records'))
            incoming = root/'incoming'
            incoming.mkdir()
            primary = incoming/'Example Work [2160p].mkv'
            primary.write_bytes(b'new version')
            task = dict(uuid='new-version', path=str(incoming), name='Example Work', is_anime=True, is_movie=True,
                movie_inventory=f.inventory_movie_assets(incoming).to_dict(), draft_target_root=str(allowed),
                draft_initial_target_root=str(allowed/'AnimeMovies'/'Example Work (2026)'),
                provider_identity=dict(namespace='bangumi', entity_type='subject', provider_id='12345', title='Example Work', year=2026),
                proposed_work_key=first.metadata.get('catalog_work_key') or first.metadata['proposed_work_key'])
            pending = f.save_plan_fixture(store, f.build_inventory_review_draft(task, dry_run=False))
            before = records(root)
            with self.assertRaisesRegex(ValueError, '相同版本标签'):
                service.complete_inventory_draft(pending.task_id, store.fingerprint(pending),
                    group_id=pending.presentation_groups[0].group_id, group_kind='movie_continuous',
                    target_root=allowed/'AnimeMovies', reason='明确新增同标签测试', root_snapshot=snapshot,
                    movie_version_groups=[dict(sources=[str(primary)], label='1080p')], persist_revision=True)
            self.assertEqual(before, records(root))
