"""核查与换源重复读取，以及新旧生成物清单兼容。"""
from collections import Counter
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.rename import asset_replacement as replacement
from src.rename import generated_outputs as outputs
from src.rename.plan_store import PlanStore
from src.rename.small_file_identity import digest_file
from src.rename.trans import Trans
from tests.test_shared_generated_ignore import SharedIgnore


class DigestReads(unittest.TestCase):
    def fixture(self, root):
        library = root / 'library'
        library.mkdir()
        source, target = root / 'source.ass', library / 'target.ass'
        source.write_bytes(b'new subtitle')
        target.write_bytes(b'old subtitle')
        return source, target, library

    def transfer(self, root):
        source, target, library = self.fixture(root)
        evidence = replacement.observe_replacement(source, target, library)
        batch = replacement.ApprovedReplacementBatch('digest-test', 'a'*64, (evidence,),
            target_root=str(library), library_roots=(str(library),))
        trans = Trans({source: target}, batch.task_id, batch.plan_fingerprint, mode='hardlink',
            dry_run=False, record_root=root/'records', operations=[dict(source=str(source), target=str(target),
                action='replace', replacement_evidence=evidence.to_dict())], replacement_authority=batch)
        return trans, source, target

    def test_prepare_reads_each_file_once(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target, library = self.fixture(Path(directory))
            with patch('src.rename.small_file_identity.digest_file', wraps=digest_file) as calls:
                evidence = replacement.observe_replacement(source, target, library)
            self.assertNotEqual(evidence.new_source, evidence.old_target)
            print('PREPARE_DIGEST_READS', len(calls.call_args_list))
            self.assertEqual(Counter([source, target]), Counter(Path(c.args[0]) for c in calls.call_args_list))

    def test_retirement_reads_once(self):
        with tempfile.TemporaryDirectory() as directory:
            _, target, library = self.fixture(Path(directory))
            with patch('src.rename.small_file_identity.digest_file', wraps=digest_file) as calls:
                evidence = replacement.observe_retirement(target, library)
            self.assertEqual(str(target), evidence.target)
            self.assertEqual(1, calls.call_count)

    def test_real_transaction_preflight_only_inside_lock(self):
        from src.rename.file_lock import advisory_file_lock
        with tempfile.TemporaryDirectory() as directory:
            trans, source, target = self.transfer(Path(directory))
            locked, checks = [], []
            original = trans._validate_replacement_state
            @contextmanager
            def lock(*a, **kw):
                with advisory_file_lock(*a, **kw) as value:
                    locked.append(True)
                    try: yield value
                    finally: locked.pop()
            def checked(*a, **kw):
                checks.append(bool(locked))
                return original(*a, **kw)
            with patch('src.rename.file_lock.advisory_file_lock', lock), patch.object(trans, '_validate_replacement_state', checked):
                self.assertIsNone(trans.trans_file())
            self.assertEqual(source.stat().st_ino, target.stat().st_ino)
            self.assertEqual('completed', json.loads((trans.record_root/'digest-test.ledger.json').read_text('utf-8'))['status'])
            print('TRANSACTION_PREFLIGHTS_LOCKED', checks)
            self.assertEqual([True], checks)

    def test_prepare_and_retirement_reject_invalid_scope_and_entity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target, library = self.fixture(root)
            for action in (lambda: replacement.observe_replacement(source, target, root/'other'),
                           lambda: replacement.observe_replacement(source, source, root),
                           lambda: replacement.observe_retirement(source, library)):
                with self.assertRaises(ValueError): action()
            original = replacement.observe_file
            def other_device(path):
                row = original(path)
                return replace(row, device=row.device+1) if Path(path) == source else row
            with patch.object(replacement, 'observe_file', other_device), self.assertRaises(ValueError):
                replacement.observe_replacement(source, target, library)

    def test_same_size_source_change_still_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            trans, source, target = self.transfer(Path(directory))
            source.write_bytes(b'bad subtitle')
            with self.assertRaises(ValueError):
                trans.trans_file()
            self.assertEqual(b'old subtitle', target.read_bytes())

    def test_target_change_and_parent_replacement_still_refused(self):
        for change_parent in (False, True):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                trans, source, target = self.transfer(root)
                if change_parent:
                    target.parent.rename(root/'retained-library')
                    target.parent.mkdir()
                    os.link(root/'retained-library'/target.name, target)
                    self.assertEqual(trans.replacement_authority.evidence[0].old_target,
                                     replacement.observe_file(target))
                else:
                    target.write_bytes(b'bad subtitle')
                with self.assertRaises(ValueError):
                    trans.trans_file()
                self.assertNotEqual(source.stat().st_ino, target.stat().st_ino)

    def test_current_validation_returns_observation_for_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target, library = self.fixture(Path(directory))
            evidence = replacement.observe_replacement(source, target, library)
            self.assertEqual((evidence.new_source, evidence.old_target), evidence.validate_current())



class GeneratedDigests(unittest.IsolatedAsyncioTestCase):
    fixture = SharedIgnore.fixture

    async def test_new_manifest_uses_xxh3_without_duplicate_file_hash(self):
        from src.rename.file_digest import digest_bytes
        from src.rename.special_catalog import encode, plain
        _, plan, _ = await self.fixture()
        manifest = plain(plan.metadata['generated_outputs'])
        self.assertEqual('generated-outputs.v2', manifest['policy'])
        self.assertNotIn('sha256', manifest['files'][0])
        self.assertEqual(digest_bytes(encode(manifest).encode('utf-8')), outputs.validate_manifest(plan))

    async def test_legacy_completion_registration_and_resume_remain_valid(self):
        from src.rename.special_catalog import encode, plain
        root, plan, _ = await self.fixture()
        manifest = plain(plan.metadata['generated_outputs'])
        manifest['policy'] = 'generated-outputs.v1'
        for row in manifest['files']:
            row['sha256'] = hashlib.sha256(row['content'].encode('utf-8')).hexdigest()
        legacy = outputs.bind_outputs(plan, manifest)
        self.assertEqual(hashlib.sha256(encode(manifest).encode('utf-8')).hexdigest(), outputs.validate_manifest(legacy))
        frozen = PlanStore.fingerprint(legacy)
        batch = outputs.approved_outputs(legacy, frozen)
        result = outputs.write_outputs(batch, task_id=legacy.task_id, fingerprint=frozen, record_root=root/'legacy-records')
        self.assertEqual('generated-outputs.v1', result['policy'])
        self.assertEqual(manifest['files'][0]['sha256'], result['files'][0]['sha256'])
        (root/'legacy-v1-plan.json').write_text(json.dumps(legacy.to_dict(), ensure_ascii=False), encoding='utf-8')
        (root/'legacy-v1-completion.json').write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
        outputs.validate_completed(legacy, {'generated_outputs': result})
        self.assertIsNotNone(outputs.resume_outputs(legacy, root/'legacy-records'))
        with self.assertRaises(ValueError):
            outputs.validate_completed(legacy, {'generated_outputs': {**result, 'policy': 'generated-outputs.v2'}})
        payload = dict(canonical_root=legacy.metadata['canonical_work_root'])
        outputs.register_outputs(payload, legacy, {'generated_outputs': result})
        outputs.validate_registered(payload)
        self.assertEqual(frozen, PlanStore.fingerprint(legacy))

    async def test_new_manifest_rejects_content_owner_and_mixed_hash_tamper(self):
        from src.rename.special_catalog import plain
        _, plan, _ = await self.fixture()
        for field, value in [('content', 'foreign'), ('owner', 'foreign'), ('sha256', '0'*64)]:
            manifest = plain(plan.metadata['generated_outputs'])
            manifest['files'][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                outputs.bind_outputs(plan, manifest)
