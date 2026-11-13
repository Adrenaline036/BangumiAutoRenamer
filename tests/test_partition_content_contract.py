"""Real default workspace partition, complete materials and guarded recovery.

All source/business writes are confined to owned TemporaryDirectory fixtures.
The provider transport is the existing frozen adapter, not a live API.
"""
from tests.source_collection_fixture import collected_identity
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.rename import content_choice
from src.rename.plan_review import entry_id
from tests.legacy_partition_builders import source_partition_entry_closure
from src.rename.plan_store import PlanStore
from src.rename.source_partition import SourcePartitionStore
from tests.legacy_partition_builders import new_partition_decision
from src.rename.task_batch_scope import TaskSourceScope
from tests import test_default_activation_contract as activation
from tests.test_content_choice_contract import change


def package(root, source):
    first = next(source.glob('*S01E01*.mkv'))
    (source / 'Ordinary Show S01E03 [1080p].mkv').write_bytes(first.read_bytes())
    first.with_suffix('.mka').write_bytes(b'disposable audio companion')
    first.with_suffix('.en.srt').write_text('1\n00:00:00,000 --> 00:00:01,000\nTest\n', encoding='utf-8')
    return source


def records(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for directory in ('tasks', 'plans', 'decisions', 'task_batch_scope', 'plan_review',
                              'source_partition', 'source_ownership', 'font_supply', 'projection_journal')
            for p in (root / directory).rglob('*.json')}


class PartitionContentContracts(unittest.IsolatedAsyncioTestCase):
    @asynccontextmanager
    async def flow(self, **kwargs):
        async with activation.DefaultActivationProducerContracts.flow(self, **kwargs) as flow:
            from src.rename.plan_review import PlanReviewService
            previous = flow.service
            flow.service = PlanReviewService(flow.store, flow.manager.review_root, flow.root / 'work_catalog',
                decision_state_root=flow.root / 'decisions', task_root=flow.root / 'tasks',
                record_root=flow.root / 'records', provider_link_catalog_root=flow.root / 'provider_links',
                root_snapshot_source=previous.root_snapshot_source)
            yield flow
    current = activation.DefaultActivationProducerContracts.current
    selected = activation.DefaultActivationProducerContracts.selected

    def video(self, plan, episode='S01E01'):
        return next(e for e in plan.entries if e.media_kind == 'video' and episode in e.source_path.name)

    async def split(self, flow, plan):
        return await flow.manager.partition_task_sources(parent_task_id=flow.task_id,
            expected_plan_fingerprint=PlanStore.fingerprint(plan),
            selected_entry_ids=[entry_id(self.video(plan))], reason='独立来源与内容继承测试')

    def link_subtitle(self, flow, plan):
        from src.rename.rename_plan import SubtitleOverrideScope
        first = self.video(plan).source_path
        subtitle = first.with_suffix('.en.srt')
        selected = next(e for e in content_choice.subtitle_edit_view(plan).entries if e.source_path == subtitle)
        plan = flow.service.apply_subtitle_override_rule(plan.task_id, PlanStore.fingerprint(plan),
            entry_id(selected), scope=SubtitleOverrideScope.FILE, associated_video_source_path=first,
            primary_language='en', script=None, reason='明确这条字幕的来源归属')
        relations = flow.service.decision_state_store.load(plan.task_id).content_selection.associations.to_dict()['relations']
        for path in (subtitle, first.with_suffix('.mka')):
            row = next(r for r in relations if r['source_path'] == str(path))
            self.assertEqual(('linked', [str(first)]), (row['state'], row['owners']))
        return plan

    async def test_closure_includes_explicitly_excluded_companions(self):
        async with self.flow(automatic=False, configure_source=package) as flow:
            plan, _ = self.current(flow)
            plan = self.link_subtitle(flow, plan)
            first = self.video(plan).source_path
            audio, subtitle = first.with_suffix('.mka'), first.with_suffix('.en.srt')
            plan = change(flow.service, plan, [audio, subtitle])
            self.assertFalse({audio, subtitle} & {e.source_path for e in plan.entries})
            videos, closure = source_partition_entry_closure(plan, [entry_id(self.video(plan))])
            self.assertEqual([first], [e.source_path for e in videos])
            self.assertEqual({first, audio, subtitle}, {e.source_path for e in closure},
                             'source transfer must include excluded but owned companions')

    async def test_retired_partition_preserves_complete_content_without_a_half_child(self):
        for confirmed in (False, True):
            with self.subTest(confirmed=confirmed):
                async with self.flow(automatic=confirmed, configure_source=package) as flow:
                    plan, state = self.current(flow)
                    self.assertEqual(confirmed, bool(state.confirmed_work))
                    plan = self.link_subtitle(flow, plan)
                    first = self.video(plan).source_path
                    audio, subtitle = first.with_suffix('.mka'), first.with_suffix('.en.srt')
                    plan = change(flow.service, plan, [audio, subtitle])
                    plan = change(flow.service, plan, [self.video(plan, 'S01E03').source_path])
                    old = flow.service.decision_state_store.load(flow.task_id)
                    old_value = old.content_selection.to_dict()
                    source_bytes = {p: p.read_bytes() for p in flow.source.rglob('*') if p.is_file()}
                    before = records(flow.root)
                    with self.assertRaisesRegex(RuntimeError, '旧二分拆卡.*预览并调整分组'):
                        await self.split(flow, plan)
                    after = records(flow.root)
                    self.assertEqual(before, after)
                    self.assertFalse(flow.manager.source_partition_store.pending_for_task(flow.task_id))
                    parent_plan, parent_state = self.current(flow)
                    self.assertEqual(old_value, parent_state.content_selection.to_dict())
                    self.assertEqual(old.confirmed_work, parent_state.confirmed_work)
                    self.assertEqual(source_bytes, {p: p.read_bytes() for p in source_bytes})
                    self.assertFalse(any(e.target_path.exists() for e in parent_plan.entries
                                         if e.target_path != e.source_path))

    async def test_manual_subtitle_history_is_preserved_when_partition_publication_is_not_available(self):
        from src.rename.rename_plan import SubtitleOverrideScope
        async with self.flow(automatic=False, configure_source=package) as flow:
            plan, _ = self.current(flow)
            first = self.video(plan).source_path
            subtitle = first.with_suffix('.en.srt')
            view = content_choice.subtitle_edit_view(plan)
            selected = next(e for e in view.entries if e.source_path == subtitle)
            plan = flow.service.apply_subtitle_override_rule(flow.task_id, PlanStore.fingerprint(plan),
                entry_id(selected), scope=SubtitleOverrideScope.FILE, associated_video_source_path=first,
                primary_language='ja', script=None, reason='只更正字幕语言')
            rule = plan.subtitle_override_rules[-1]
            history = flow.store.load_version(flow.task_id, rule.base_plan_fingerprint)
            history_bytes = flow.store._history_path(flow.task_id, rule.base_plan_fingerprint).read_bytes()
            records_before = records(flow.root)
            with self.assertRaisesRegex(RuntimeError, '旧二分拆卡.*预览并调整分组'):
                await self.split(flow, plan)
            after = records(flow.root)
            self.assertEqual(records_before, after)
            self.assertFalse(flow.manager.source_partition_store.pending_for_task(flow.task_id))
            current = flow.store.load(flow.task_id)
            self.assertEqual('ja', next(e.subtitle.primary_language for e in content_choice.subtitle_edit_view(current).entries
                                        if e.source_path == subtitle))
            self.assertEqual(history_bytes, flow.store._history_path(flow.task_id, rule.base_plan_fingerprint).read_bytes())
            self.assertEqual(flow.task_id, history.task_id)  # no forged child historical plan

    async def test_execution_risk_is_rejected_before_any_partition_write(self):
        for risk in ({'workflow_status': 'completed'}, {'execution_may_have_written_targets': True},
                     {'execution_recovery_disposition': 'manual_check'}):
            with self.subTest(risk=risk):
                async with self.flow(automatic=False, configure_source=package) as flow:
                    plan, _ = self.current(flow)
                    flow.manager._update(flow.task_id, **risk)
                    before = records(flow.root)
                    with patch.object(flow.manager.source_partition_store, 'prepare',
                                      wraps=flow.manager.source_partition_store.prepare) as prepared:
                        with self.assertRaises((ValueError, RuntimeError)):
                            await self.split(flow, plan)
                    self.assertFalse(prepared.called, 'execution risk must reject before preparing a transaction')
                    self.assertEqual(before, records(flow.root))

    async def test_unresolved_subtitle_is_not_guessed_into_video_closure(self):
        async with self.flow(automatic=False, configure_source=package) as flow:
            plan, state = self.current(flow)
            first = self.video(plan).source_path
            subtitle = first.with_suffix('.en.srt')
            row = next(r for r in state.content_selection.associations.to_dict()['relations']
                       if r['source_path'] == str(subtitle))
            self.assertEqual(('unresolved', []), (row['state'], row['owners']))
            _, closure = source_partition_entry_closure(plan, [entry_id(self.video(plan))])
            self.assertNotIn(subtitle, {e.source_path for e in closure})

    async def test_shared_typed_companion_requires_all_owners_and_video_order_is_preserved(self):
        from src.rename.content_associations import ContentAssociations, partition_source_closure
        async with self.flow(automatic=False, configure_source=package) as flow:
            plan, state = self.current(flow)
            first, second = self.video(plan), self.video(plan, 'S01E02')
            videos, _ = source_partition_entry_closure(plan, [entry_id(second), entry_id(first)])
            self.assertEqual([second.source_path, first.source_path], [e.source_path for e in videos])
            with self.assertRaises((ValueError, RuntimeError)):
                source_partition_entry_closure(plan, [entry_id(first), entry_id(first)])
            # Pure relation contract, not a manufactured accepted Plan.
            value = state.content_selection.associations.to_dict()
            audio = first.source_path.with_suffix('.mka')
            row = next(r for r in value['relations'] if r['source_path'] == str(audio))
            row.update(state='shared', owners=sorted([str(first.source_path), str(second.source_path)]), source='manual')
            shared = ContentAssociations.from_mapping(value)
            with self.assertRaisesRegex(ValueError, '共享'):
                partition_source_closure(shared, [first.source_path])
            self.assertEqual({first.source_path, second.source_path, audio},
                             set(partition_source_closure(shared, [second.source_path, first.source_path])))

    async def test_healthy_unapproved_task_cannot_reach_retired_prepare(self):
        async with self.flow(automatic=False, configure_source=package) as flow:
            plan, _ = self.current(flow)
            before = records(flow.root)
            with patch.object(flow.manager.source_partition_store, 'prepare',
                              side_effect=RuntimeError('bounded prepare reached')) as prepared:
                with self.assertRaisesRegex(RuntimeError, '旧二分拆卡.*预览并调整分组'):
                    await self.split(flow, plan)
            prepared.assert_not_called()
            self.assertEqual(before, records(flow.root))

    async def test_actual_approved_plan_is_rejected_before_prepare(self):
        async with self.flow(automatic=True, configure_source=package) as flow:
            plan, _ = self.current(flow)
            plan = self.link_subtitle(flow, plan)
            review = flow.service.approve_entries(plan.task_id, PlanStore.fingerprint(plan),
                                                  [entry_id(e) for e in plan.entries])
            self.assertTrue(review.approved_entry_ids)
            before = records(flow.root)
            with patch.object(flow.manager.source_partition_store, 'prepare',
                              wraps=flow.manager.source_partition_store.prepare) as prepared:
                with self.assertRaises((ValueError, RuntimeError)):
                    await self.split(flow, plan)
            self.assertFalse(prepared.called)
            self.assertEqual(before, records(flow.root))



class PartitionJournalContracts(unittest.TestCase):
    def setup_store(self, root):
        from src.task_queue import TaskQueueManager
        # Actual source identity, not an invented device/inode fingerprint.
        source = root / 'source.mkv'
        source.write_bytes(b'disposable physical source')
        identity = collected_identity(source)
        decision = new_partition_decision(parent_task_id='parent', child_task_id='child',
            expected_parent_plan_fingerprint='1' * 64, expected_parent_batch_id='batch',
            expected_parent_scope_fingerprint='2' * 64, expected_parent_reservation_revision=1,
            selected_source_identities=(identity,), reason='恢复时保全后来写入')
        store = SourcePartitionStore(root / 'partition', allowed_roots=[root / 'data'])
        (root / 'data').mkdir()
        paths = [root / 'data' / name for name in ('task.json', 'plan.json')]
        for path in paths:
            path.write_bytes(b'original')
        from tests.legacy_partition_fixture import seed_history
        seed_history(store, decision, paths)
        return store, decision, paths

    def test_unproven_later_bytes_are_preserved_and_recovery_remains_pending(self):
        with tempfile.TemporaryDirectory() as raw:
            store, decision, paths = self.setup_store(Path(raw))
            paths[0].write_bytes(b'later independent task revision')
            paths[1].write_bytes(b'later independent plan revision')
            before = [p.read_bytes() for p in paths]
            fresh = SourcePartitionStore(store.root, allowed_roots=store.allowed_roots)
            for _ in range(2):
                with self.assertRaises(RuntimeError):
                    fresh.recover_pending()
                self.assertEqual(before, [p.read_bytes() for p in paths])
                self.assertTrue(fresh.pending_for_task('parent'))

    def test_invalid_backup_anywhere_prevents_restoring_any_other_target(self):
        with tempfile.TemporaryDirectory() as raw:
            store, decision, paths = self.setup_store(Path(raw))
            journal = store.transaction_path(decision.partition_id)
            payload = json.loads(journal.read_text('utf-8'))
            payload['backups'][0]['content_b64'] = None
            journal.write_text(json.dumps(payload), encoding='utf-8')
            paths[1].write_bytes(b'must survive whole-set validation')
            before = [p.read_bytes() for p in paths]
            with self.assertRaises(RuntimeError):
                store.rollback(decision.partition_id)
            self.assertEqual(before, [p.read_bytes() for p in paths])

    def test_transaction_must_match_immutable_decision_before_recovery(self):
        with tempfile.TemporaryDirectory() as raw:
            store, decision, paths = self.setup_store(Path(raw))
            journal = store.transaction_path(decision.partition_id)
            payload = json.loads(journal.read_text('utf-8'))
            payload['decision_fingerprint'] = '0' * 64
            journal.write_text(json.dumps(payload), encoding='utf-8')
            before = [p.read_bytes() for p in paths]
            with self.assertRaises(RuntimeError):
                store.rollback(decision.partition_id)
            self.assertEqual(before, [p.read_bytes() for p in paths])

    def test_unchanged_legacy_preimages_and_absence_close_without_rewriting(self):
        with tempfile.TemporaryDirectory() as raw:
            store, decision, paths = self.setup_store(Path(raw))
            absent = paths[0].with_name('absent.json')
            from tests.legacy_partition_fixture import append_history
            append_history(store, decision.partition_id, absent)
            before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in paths]
            self.assertEqual({decision.partition_id: 'failed_closed'}, store.recover_pending())
            self.assertEqual({}, store.recover_pending())
            store.rollback(decision.partition_id)
            self.assertEqual(before, [(p.read_bytes(), p.stat().st_mtime_ns) for p in paths])
            self.assertFalse(absent.exists())
            self.assertFalse(store.pending_for_task('parent'))

    def test_missing_decision_duplicate_unsafe_path_and_new_target_all_preserved(self):
        for attack in ('missing_decision', 'wrong_parent', 'duplicate', 'outside', 'absent_appeared'):
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                store, decision, paths = self.setup_store(root)
                journal = store.transaction_path(decision.partition_id)
                absent = paths[0].with_name('absent.json')
                from tests.legacy_partition_fixture import append_history
                append_history(store, decision.partition_id, absent)
                payload = json.loads(journal.read_text('utf-8'))
                if attack == 'missing_decision':
                    store.decision_path(decision.partition_id).unlink()
                elif attack == 'wrong_parent':
                    payload['parent_task_id'] = 'another-parent'
                elif attack == 'duplicate':
                    payload['backups'].append(dict(payload['backups'][0]))
                elif attack == 'outside':
                    payload['backups'][0]['path'] = str(root / 'outside.json')
                else:
                    absent.write_bytes(b'independent new publication')
                journal.write_text(json.dumps(payload), encoding='utf-8')
                before = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
                with self.assertRaises(RuntimeError):
                    store.rollback(decision.partition_id)
                self.assertEqual(before, {p: p.read_bytes() for p in root.rglob('*') if p.is_file()})

    def test_cut_backup_rows_cannot_release_pending_protection(self):
        for clear in (False, True):
            with self.subTest(clear=clear), tempfile.TemporaryDirectory() as raw:
                store, decision, paths = self.setup_store(Path(raw))
                paths[0].write_bytes(b'later writer must remain protected')
                journal = store.transaction_path(decision.partition_id)
                payload = json.loads(journal.read_text('utf-8'))
                payload['backups'] = [] if clear else payload['backups'][1:]
                journal.write_text(json.dumps(payload), encoding='utf-8')
                before = {p: p.read_bytes() for p in Path(raw).rglob('*') if p.is_file()}
                with self.assertRaises(RuntimeError):
                    store.rollback(decision.partition_id)
                self.assertEqual(before, {p: p.read_bytes() for p in Path(raw).rglob('*') if p.is_file()})
                self.assertTrue(store.pending_for_task('parent'))

    def test_missing_complete_set_proof_does_not_upgrade_a_legacy_journal(self):
        with tempfile.TemporaryDirectory() as raw:
            store, decision, paths = self.setup_store(Path(raw))
            # Actual v1 shape, without the newly recorded independent authority.
            base = store.backup_root / decision.partition_id / 'base.json'
            base.unlink()
            before = {p: p.read_bytes() for p in Path(raw).rglob('*') if p.is_file()}
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, '完整恢复清单'):
                    store.rollback(decision.partition_id)
                self.assertEqual(before, {p: p.read_bytes() for p in Path(raw).rglob('*') if p.is_file()})
                self.assertTrue(store.pending_for_task('parent'))
                self.assertFalse(base.exists(), 'must not attest current mutable rows as historical proof')

    def test_appended_backup_cannot_be_cut_or_rebound_by_mutable_journal(self):
        for attack in ('cut', 'replace_bytes', 'wrong_base', 'missing_extension', 'append_window'):
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as raw:
                store, decision, paths = self.setup_store(Path(raw))
                journal = store.transaction_path(decision.partition_id)
                old = json.loads(journal.read_text('utf-8'))
                added = paths[0].with_name('new-history.json')
                from tests.legacy_partition_fixture import append_history
                append_history(store, decision.partition_id, added)
                payload = json.loads(journal.read_text('utf-8'))
                if attack == 'cut':
                    payload['backups'] = old['backups']
                elif attack == 'replace_bytes':
                    import base64
                    paths[0].write_bytes(b'later bytes')
                    payload['backups'][0]['content_b64'] = base64.b64encode(b'later bytes').decode('ascii')
                elif attack == 'wrong_base':
                    base = store.backup_root / decision.partition_id / 'base.json'
                    value = json.loads(base.read_text('utf-8'))
                    value['decision_fingerprint'] = '0' * 64
                    base.write_text(json.dumps(value), encoding='utf-8')
                elif attack == 'missing_extension':
                    next((store.backup_root / decision.partition_id / 'extensions').glob('*.json')).unlink()
                else:
                    payload = old  # crash after immutable append but before journal publication
                journal.write_text(json.dumps(payload), encoding='utf-8')
                before = {p: p.read_bytes() for p in Path(raw).rglob('*') if p.is_file()}
                with self.assertRaises(RuntimeError):
                    store.rollback(decision.partition_id)
                self.assertEqual(before, {p: p.read_bytes() for p in Path(raw).rglob('*') if p.is_file()})
                self.assertTrue(store.pending_for_task('parent'))


if __name__ == '__main__':
    unittest.main()
