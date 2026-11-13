"""ERR060 accepted-contract incidents. Only disposable test roots are mutated."""

from tests._historical_replacement import historical_replacement
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.rename.media_classifier import classify_media, LocalRole
from src.rename.plan_store import PlanStore
from tests import test_asset_actions_contract as action_fixture


class ClassificationIncidents(unittest.TestCase):
    def test_nonvideo_numeric_names_never_become_episodes(self):
        for name in ('Scan/img001.bmp', '特典CD (Vol.1)/Soundtrack vol.1.cue',
                     '特典CD (Vol.1)/Soundtrack vol.1.log',
                     '特典CD (Vol.1)/Soundtrack vol.1.wav'):
            with self.subTest(name=name):
                path = Path(name)
                observed = classify_media(path, path.stem, 1)
                self.assertNotEqual(LocalRole.MAIN_EPISODE, observed.local_role)
                self.assertIsNone(observed.episode_number)

    def test_regular_video_control(self):
        result = classify_media(Path('Show 01.mkv'), 'Show 01', 1)
        self.assertEqual((LocalRole.MAIN_EPISODE, 1), (result.local_role, result.episode_number))




class WholeVersionReplacementIncidents(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.helper = action_fixture.AssetActionContracts('runTest')
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)

    @historical_replacement
    async def test_failed_replacement_preserves_original_error_and_validates_failure_ledger(self):
        from src.rename.plan_execution import PlanExecutionError
        old = await self.helper.fixture.single(with_font=False)
        old_targets = {e.target_path: e.target_path.read_bytes() for e in old.plan.entries}
        incoming = await self.helper.incoming()
        review = self.helper.fixture.execution_service().review_service
        operation, _ = review.asset_actions().prepare_replacement(incoming)
        plan = review.confirm_asset_replacement(incoming.task_id, PlanStore.fingerprint(incoming),
            expected_operation_fingerprint=operation.fingerprint)
        target = Path(operation.to_dict()['replaces'][0]['target'])
        original_replace = os.replace
        marker = 'ERR060：模拟文件访问被拒绝'
        def deny_publish(source, destination):
            if Path(destination) == target:
                raise PermissionError(marker)
            return original_replace(source, destination)
        catalog_before = {p: p.read_bytes() for p in (self.helper.root / 'work_catalog').glob('*.json')}
        with patch('src.rename.trans.os.replace', side_effect=deny_publish):
            task, ledger, service = await self.helper.fixture.harness._approve_and_execute(
                root=self.helper.root, result=self.helper.fixture.current(plan.task_id))
        self.assertEqual('rolled_back', ledger['status'])
        self.assertEqual('failed', task['execution_status'])
        self.assertEqual(old_targets, {p: p.read_bytes() for p in old_targets})
        self.assertEqual(catalog_before, {p: p.read_bytes() for p in catalog_before})
        # Validate identity on a failed ledger too; this must not become a bypass.
        ledger_path = self.helper.root / 'records' / f'{plan.task_id}.ledger.json'
        ledger['task_id'] = 'foreign-task'
        ledger_path.write_text(json.dumps(ledger), encoding='utf-8')
        with self.assertRaisesRegex(PlanExecutionError, 'task_id'):
            service._load_ledger(ledger_path, plan.task_id, PlanStore.fingerprint(plan), plan)
        self.assertIn(marker, task.get('error', ''))
        self.assertNotIn('碰撞处置不一致', task.get('error', ''))

    async def test_whole_season_zero_overlap_retires_old_extras_and_preserves_other_season(self):
        from tests import _media_flow_helpers as tv_fixture
        from src.rename.series_catalog import SeriesCatalog
        root = self.helper.root
        harness = tv_fixture.MediaFlow('runTest')
        source, _archive = tv_fixture.make_season(root / 'old-s1')
        (source / 'NCOP.mkv').write_bytes(next(source.glob('*.mkv')).read_bytes())
        old = await harness._run_flow(root=root, source=source, task_id='old-s1', subject_id='41658',
            confirm_fixture_extras=(source / 'NCOP.mkv',))
        old_done, _, _ = await harness._approve_and_execute(root=root, result=old)
        self.assertEqual('completed', old_done['execution_status'], old_done.get('error'))
        self.assertTrue(any('Extras' in e.target_path.parts for e in old.plan.entries))
        peer_source, _peer_archive = tv_fixture.make_season(root / 'peer-s2', second=True)
        peer = await harness._run_flow(root=root, source=peer_source, task_id='peer-s2', subject_id='86670')
        peer_done, _, _ = await harness._approve_and_execute(root=root, result=peer)
        self.assertEqual('completed', peer_done['execution_status'], peer_done.get('error'))
        peer_identity = {e.target_path: (e.target_path.stat().st_ino, e.target_path.read_bytes())
                         for e in peer.plan.entries}
        new_source, archive = tv_fixture.make_season(root / 'new-s1')
        archive.unlink()
        for path in tuple(new_source.glob('*.ass')):
            path.unlink()
        # Source observations change before admission; no downstream Plan edits.
        # The container bytes are a transport fixture, not playback acceptance.
        for path in tuple(new_source.glob('*.mkv')):
            path.rename(path.with_name(path.name.replace('VCB-Studio', 'New-Group')).with_suffix('.mp4'))
        originals = {p: p.read_bytes() for directory in (source, peer_source, new_source)
                     for p in directory.rglob('*') if p.is_file()}
        incoming = await harness._run_flow(root=root, source=new_source, task_id='new-s1', subject_id='41658')
        old_targets = {e.target_path for e in old.plan.entries}
        self.assertFalse(old_targets & {e.target_path for e in incoming.plan.entries})
        review = self.helper.fixture.execution_service().review_service
        operation, _ = review.asset_actions().prepare_replacement(incoming.plan)
        plan = review.confirm_asset_replacement(incoming.plan.task_id, PlanStore.fingerprint(incoming.plan),
            expected_operation_fingerprint=operation.fingerprint)
        task, ledger, _ = await harness._approve_and_execute(root=root, result=self.helper.fixture.current(plan.task_id))
        self.assertEqual('completed', task['execution_status'], task.get('error'))
        self.assertEqual('completed', ledger['status'])
        self.assertTrue(all(e.target_path.exists() for e in plan.entries))
        self.assertEqual(peer_identity, {p: (p.stat().st_ino, p.read_bytes()) for p in peer_identity})
        self.assertEqual(originals, {p: p.read_bytes() for p in originals})
        self.assertFalse([str(p) for p in old_targets if p.exists()], '旧S1零同名资产必须全部退场')
        catalog = SeriesCatalog(root / 'series_catalog').load(plan.metadata['series_key'])
        self.assertNotIn(old.plan.task_id, {r['task_id'] for r in catalog.season_bindings})
        self.assertNotIn('retain', {r['disposition'] for r in plan.metadata['asset_operation']['previous_assets']})

    async def test_replacing_version_retires_old_exclusive_companion(self):
        old = await self.helper.fixture.single(with_font=False)
        companion = next(e for e in old.plan.entries if e.source_path.suffix == '.mka')
        source_bytes = companion.source_path.read_bytes()
        incoming = await self.helper.incoming(omit_companion=True)
        new_plan = await self.helper.complete_replacement(incoming)
        self.assertFalse(companion.target_path.exists(), 'old-only companion must retire with its version')
        self.assertEqual(source_bytes, companion.source_path.read_bytes())
        operation = new_plan.metadata['asset_operation']
        self.assertFalse(any(row['disposition'] == 'retain' for row in operation['previous_assets']))

    @historical_replacement
    async def test_public_rollback_restores_committed_version_and_is_idempotent(self):
        old = await self.helper.fixture.single(with_font=False)
        companion = next(e for e in old.plan.entries if e.source_path.suffix == '.mka')
        old_bytes = companion.target_path.read_bytes()
        incoming = await self.helper.incoming(omit_companion=True)
        current = await self.helper.complete_replacement(incoming)
        execution = self.helper.fixture.execution_service()
        prepare = getattr(execution, 'prepare_asset_rollback', None)
        rollback = getattr(execution, 'rollback_asset_replacement', None)
        self.assertTrue(callable(prepare) and callable(rollback), 'public rollback command is missing')
        fingerprint = PlanStore.fingerprint(current)
        prepared = prepare(current.task_id, fingerprint)
        rollback(current.task_id, fingerprint, expected_recovery_fingerprint=prepared.fingerprint)
        self.assertEqual(old_bytes, companion.target_path.read_bytes())
        prepared = prepare(current.task_id, fingerprint)
        rollback(current.task_id, fingerprint, expected_recovery_fingerprint=prepared.fingerprint)
        self.assertEqual(old_bytes, companion.target_path.read_bytes())

    @historical_replacement
    async def test_public_rollback_resumes_every_durable_phase_without_new_source(self):
        import src.rename.asset_rollback as inverse
        old = await self.helper.fixture.single(with_font=False)
        old_identity = {e.target_path: (e.target_path.stat().st_ino, e.target_path.read_bytes())
                        for e in old.plan.entries}
        incoming = await self.helper.incoming(omit_companion=True)
        current = await self.helper.complete_replacement(incoming)
        # A transport fixture may disappear after replacement; restoration only
        # needs the owned old backup, never the new download or its directory.
        for entry in current.entries:
            if entry.source_path.exists():
                entry.source_path.unlink()
        execution = self.helper.fixture.execution_service()
        fp = PlanStore.fingerprint(current)
        original = inverse._write_journal
        class Interrupted(BaseException):
            pass
        for phase in ('rollback_prepared', 'media_restored', 'catalog_restored', 'fonts_restored'):
            with self.subTest(phase=phase):
                prepared = execution.prepare_asset_rollback(current.task_id, fp)
                def interrupt(path, preparation, written_phase):
                    original(path, preparation, written_phase)
                    if written_phase == phase:
                        raise Interrupted(phase)
                with patch.object(inverse, '_write_journal', side_effect=interrupt):
                    with self.assertRaises(Interrupted):
                        execution.rollback_asset_replacement(current.task_id, fp,
                            expected_recovery_fingerprint=prepared.fingerprint)
                execution = self.helper.fixture.execution_service()
        prepared = execution.prepare_asset_rollback(current.task_id, fp)
        execution.rollback_asset_replacement(current.task_id, fp,
            expected_recovery_fingerprint=prepared.fingerprint)
        self.assertEqual(old_identity, {p: (p.stat().st_ino, p.read_bytes()) for p in old_identity})

    @historical_replacement
    async def test_public_queue_rollback_preserves_later_other_version_and_refuses_old_generation(self):
        import asyncio
        from src.task_queue import TaskQueueManager
        from src.rename.asset_rollback import prepare
        old = await self.helper.fixture.single(with_font=False)
        old_targets = {e.target_path: (e.target_path.stat().st_ino, e.target_path.read_bytes()) for e in old.plan.entries}
        replacement = await self.helper.complete_replacement(await self.helper.incoming(omit_companion=True))
        peer = await self.helper.incoming('2160p', generation='later-peer')
        peer_task, _, _ = await self.helper.fixture.harness._approve_and_execute(root=self.helper.root,
            result=self.helper.fixture.current(peer.task_id))
        self.assertEqual('completed', peer_task['execution_status'])
        peer_targets = {e.target_path: (e.target_path.stat().st_ino, e.target_path.read_bytes()) for e in peer.entries}
        service = self.helper.fixture.execution_service()
        fp = PlanStore.fingerprint(replacement)
        prepared = prepare(service, replacement.task_id, fp)
        manager = TaskQueueManager(task_root=self.helper.root/'tasks', execution_service=service,
            decision_state_root=self.helper.root/'decisions', recovery_abandon_audit_root=self.helper.root/'abandon_audit',
            plan_root=self.helper.root/'plans')
        await manager.start()
        try:
            await manager.enqueue_asset_rollback(replacement.task_id, fp,
                expected_recovery_fingerprint=prepared.fingerprint)
            await asyncio.wait_for(manager._queue.join(), timeout=15)
        finally:
            await manager.stop()
        task = json.loads((self.helper.root/'tasks'/f'{replacement.task_id}.json').read_text('utf-8'))
        self.assertEqual('rolled_back',task['workflow_status'],task.get('error'))
        self.assertEqual(old_targets,{p:(p.stat().st_ino,p.read_bytes()) for p in old_targets})
        self.assertEqual(peer_targets,{p:(p.stat().st_ino,p.read_bytes()) for p in peer_targets})
        newer = await self.helper.complete_replacement(await self.helper.incoming(generation='later-same-slot'))
        with self.assertRaisesRegex(ValueError,'代际|后续'):
            prepare(service,replacement.task_id,fp)
        self.assertTrue(all(e.target_path.exists() for e in newer.entries))





if __name__ == '__main__':
    unittest.main()
