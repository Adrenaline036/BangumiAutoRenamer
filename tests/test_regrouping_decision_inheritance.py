"""拆卡首建保留逐文件人工决定，隔离来源与固定资料传输。"""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import zipfile

from src.rename import content_choice, default_content
from src.rename.plan_review import entry_id
from src.rename.plan_store import PlanStore
from src.rename.rename_plan import SubtitleOverrideScope
from src.rename.recognition_queue_admission import capture_workspace_open
from src.rename.task_batch_scope import TaskSourceScope
from tests import test_partition_content_contract as fixture
from tests import test_source_regrouping_preview_contract as preview
from tests.test_content_choice_contract import change


def report(label, **value):
    print('PROBE ' + json.dumps(dict(label=label, **value), ensure_ascii=False, sort_keys=True), flush=True)


class RegroupingInheritance(unittest.IsolatedAsyncioTestCase):
    flow = fixture.PartitionContentContracts.flow
    current = fixture.PartitionContentContracts.current
    video = fixture.PartitionContentContracts.video

    async def confirm(self, flow, task_id):
        opened = capture_workspace_open(flow.manager, task_id, flow.manager._read(task_id))
        await flow.manager.confirm_source_group(task_id, opened_evidence=opened)
        await asyncio.wait_for(flow.manager._queue.join(), 12)
        task = flow.manager._read(task_id)
        self.assertTrue(flow.store.path_for(task_id).exists(), task.get('error'))
        return flow.store.load(task_id), flow.service.decision_state_store.load(task_id)

    async def restart(self, flow):
        from src.task_queue import TaskQueueManager
        from src.rename.plan_review import PlanReviewService
        old = flow.manager
        await old.stop()
        manager = TaskQueueManager(task_root=old.task_root, plan_root=old.plan_store.root,
            decision_state_root=old.decision_state_root,
            provider_link_catalog_root=old.provider_link_catalog_root,
            recovery_abandon_audit_root=old.recovery_abandon_audit_root)
        manager.root_snapshot_source = old.root_snapshot_source
        flow.manager = manager
        flow.addAsyncCleanup(manager.stop)
        flow.service = PlanReviewService(flow.store, manager.review_root, flow.root/'work_catalog',
            decision_state_root=flow.root/'decisions', task_root=flow.root/'tasks',
            record_root=flow.root/'records', provider_link_catalog_root=flow.root/'provider_links',
            root_snapshot_source=manager.root_snapshot_source)
        await manager.start()

    def selected_child(self, flow, ids, path):
        return next(task_id for task_id in ids if str(path) in {
            row.source_path for row in TaskSourceScope.from_mapping(
                flow.manager._read(task_id)['task_source_scope']).source_identities})

    async def test_all_sources_can_remain_in_one_group_with_manual_subtitle(self):
        def worker(value):
            self.worker = value
        async with self.flow(automatic=False, configure_source=fixture.package, configure_worker=worker) as flow:
            plan, state = self.current(flow)
            video = self.video(plan).source_path
            subtitle = video.with_suffix('.en.srt')
            selected = next(e for e in content_choice.subtitle_edit_view(plan).entries if e.source_path == subtitle)
            plan = flow.service.apply_subtitle_override_rule(plan.task_id, PlanStore.fingerprint(plan),
                entry_id(selected), scope=SubtitleOverrideScope.FILE, associated_video_source_path=video,
                primary_language='ja', script=None, additional_languages=('en',), reason='单组保留人工字幕')
            state = flow.service.decision_state_store.load(plan.task_id)
            paths = [r['source_path'] for r in state.content_selection.to_dict()['materials']]
            preview = flow.manager.preview_source_regrouping(task_id=plan.task_id,
                expected_plan_fingerprint=PlanStore.fingerprint(plan), groups=[paths])
            ids = flow.manager.create_regrouped_tasks(preview)
            self.assertEqual(1, len(ids))
            self.worker.search.get_tv_info = lambda *_: self.worker.search.get_tv_info_by_id('4242')
            child, after = await self.confirm(flow, ids[0])
            contract = next(e.subtitle for e in content_choice.subtitle_edit_view(child).entries if e.source_path == subtitle)
            self.assertEqual(('ja', ('en',)), (contract.primary_language, contract.additional_languages))
            self.assertEqual(set(paths), {r['source_path'] for r in after.content_selection.to_dict()['materials']})
            self.assertFalse(list(flow.manager.execution_service.record_root.glob('*.json')))

    async def test_manual_subtitle_and_ordinary_video_exclusion_survive(self):
        def worker(value):
            self.worker = value
        def same_season(root, value):
            mixed = preview.five_seasons(root, value)
            first = next((mixed / 'Season 1').glob('*.mkv'))
            second = first.with_name(first.name.replace('S01E01', 'S01E02'))
            second.write_bytes(first.read_bytes())
            return mixed
        async with self.flow(automatic=False, configure_source=same_season,
                             configure_worker=worker) as flow:
            plan, state = self.current(flow)
            first = self.video(plan).source_path
            subtitle = first.with_suffix('.en.srt')
            selected = next(e for e in content_choice.subtitle_edit_view(plan).entries
                            if e.source_path == subtitle)
            plan = flow.service.apply_subtitle_override_rule(plan.task_id, PlanStore.fingerprint(plan),
                entry_id(selected), scope=SubtitleOverrideScope.FILE, associated_video_source_path=first,
                primary_language='ja', script=None, additional_languages=('en',),
                reason='拆卡继承：逐文件人工语言')
            second = next(e.source_path for e in plan.entries if e.media_kind == 'video' and 'S01E02' in e.source_path.name)
            plan = change(flow.service, plan, [second, subtitle])
            before_state = flow.service.decision_state_store.load(plan.task_id)
            before_subtitle = next(e for e in content_choice.subtitle_edit_view(plan).entries if e.source_path == subtitle)
            self.assertEqual('ja', before_subtitle.subtitle.primary_language)
            self.assertEqual(('en',), before_subtitle.subtitle.additional_languages)
            self.assertIn(str(second), before_state.content_selection.to_dict()['excluded_sources'])
            original = {str(p): p.read_bytes() for p in flow.source.rglob('*') if p.is_file()}
            proposed = flow.manager.preview_source_regrouping(task_id=flow.task_id,
                expected_plan_fingerprint=PlanStore.fingerprint(plan))
            ids = flow.manager.create_regrouped_tasks(proposed)
            self.worker.search.get_tv_info = lambda *_: self.worker.search.get_tv_info_by_id('4242')
            first_id = self.selected_child(flow, ids, first)
            second_id = self.selected_child(flow, ids, second)
            first_plan, first_state = await self.confirm(flow, first_id)
            self.assertEqual(first_id, second_id)
            second_plan, second_state = first_plan, first_state
            self.assertEqual('need_confirm', flow.manager._read(first_id)['workflow_status'])
            after_subtitle = next(e for e in content_choice.subtitle_edit_view(first_plan).entries if e.source_path == subtitle)
            report('字幕与排除', created_cards=len(ids), parent_language=before_subtitle.subtitle.primary_language,
                child_language=after_subtitle.subtitle.primary_language,
                parent_additional=list(before_subtitle.subtitle.additional_languages),
                child_additional=list(after_subtitle.subtitle.additional_languages),
                parent_subtitle_excluded=str(subtitle) in before_state.content_selection.to_dict()['excluded_sources'],
                child_subtitle_excluded=str(subtitle) in first_state.content_selection.to_dict()['excluded_sources'],
                parent_ordinary_video_excluded=str(second) in before_state.content_selection.to_dict()['excluded_sources'],
                child_ordinary_video_excluded=str(second) in second_state.content_selection.to_dict()['excluded_sources'],
                parent_rule_count=len(plan.subtitle_override_rules), child_rule_count=len(first_plan.subtitle_override_rules),
                child_statuses=[flow.manager._read(x)['workflow_status'] for x in (first_id, second_id)])
            self.assertEqual('ja', after_subtitle.subtitle.primary_language)
            self.assertEqual(('en',), after_subtitle.subtitle.additional_languages)
            self.assertIn(str(subtitle), first_state.content_selection.to_dict()['excluded_sources'])
            self.assertIn(str(second), second_state.content_selection.to_dict()['excluded_sources'])
            self.assertEqual(0, len(first_plan.subtitle_override_rules))
            self.assertEqual(original, {p: Path(p).read_bytes() for p in original})
            self.assertFalse(list(flow.manager.execution_service.record_root.glob('*.json')))

    async def test_discovered_movie_type_keeps_birth_choices_after_scope_rebinding(self):
        def source(root, value):
            video = next(value.glob('*[[]1080p[]].mkv'))
            extra = value / 'SPs' / 'Ordinary Movie [IV].mkv'
            extra.parent.mkdir()
            extra.write_bytes(video.read_bytes())
            extra.with_suffix('.en.srt').write_text('1\n00:00:00,000 --> 00:00:01,000\nTest\n', encoding='utf8')
            return value
        def worker(value):
            self.worker = value
        async with self.flow(movie=True, automatic=True, configure_source=source, configure_worker=worker) as flow:
            plan, state = self.current(flow)
            video = flow.source / 'SPs' / 'Ordinary Movie [IV].mkv'
            subtitle = video.with_suffix('.en.srt')
            item = next(e for e in content_choice.subtitle_edit_view(plan).entries if e.source_path == subtitle)
            plan = flow.service.apply_subtitle_override_rule(plan.task_id, PlanStore.fingerprint(plan),
                entry_id(item), scope=SubtitleOverrideScope.FILE, associated_video_source_path=video,
                primary_language='ja', script=None, additional_languages=('en',), reason='人工字幕决定')
            plan = change(flow.service, plan, [video])
            paths = [r['source_path'] for r in flow.service.decision_state_store.load(plan.task_id).content_selection.to_dict()['materials']]
            chosen = [str(video), str(subtitle)]
            proposed = flow.manager.preview_source_regrouping(task_id=plan.task_id,
                expected_plan_fingerprint=PlanStore.fingerprint(plan), groups=[chosen, [p for p in paths if p not in chosen]])
            ids = flow.manager.create_regrouped_tasks(proposed)
            child = self.selected_child(flow, ids, video)
            birth = flow.manager._read(child)['source_decision_transfer']
            self.assertEqual('tv', birth['child_scope']['media_domain'])
            lookup = self.worker.search.get_movie_info
            self.worker.search.get_movie_info = lambda *_: lookup('Ordinary Movie')
            self.worker.search.get_tv_info = lambda *_: ('', None)
            self.worker.bangumi_search = None
            child_plan, child_state = await self.confirm(flow, child)
            current = flow.manager._read(child)
            self.assertEqual('movie', current['task_source_scope']['media_domain'])
            self.assertEqual(birth, current['source_decision_transfer'])
            self.assertEqual(birth['child_scope']['source_identity_set_fingerprint'], current['task_source_scope']['source_identity_set_fingerprint'])
            rows = child_state.content_selection.to_dict()['materials']
            contract = next(r['subtitle'] for r in rows if r['source_path'] == str(subtitle))
            self.assertEqual(('ja', ['en']), (contract['primary_language'], contract['additional_languages']))
            self.assertIn(str(video), child_state.content_selection.to_dict()['excluded_sources'])
            self.assertFalse(list(flow.manager.execution_service.record_root.glob('*.json')))


    async def test_two_cards_keep_separate_choices_and_later_changes_after_reentry(self):
        def sources(root, source):
            seed = next(source.glob('*S01E01*.mkv')).read_bytes()
            for episode in (3, 4):
                (source / f'Ordinary Show S01E{episode:02} [1080p].mkv').write_bytes(seed)
            for episode in (1, 3):
                video = next(source.glob(f'*S01E{episode:02}*.mkv'))
                video.with_suffix('.en.srt').write_text('1\n00:00:00,000 --> 00:00:01,000\nTest\n', encoding='utf-8')
                with zipfile.ZipFile(source / f'Manual{episode}.zip', 'w') as archive:
                    archive.writestr('Fixture.ttf', b'excluded archive member')
            return source
        def worker(value):
            self.worker = value
        async with self.flow(automatic=True, configure_source=sources, configure_worker=worker) as flow:
            plan, _ = self.current(flow)
            specs, groups = [], []
            for episode, language, additional in ((1, 'ja', ('en',)), (3, 'en', ('ja',))):
                video = next(e.source_path for e in plan.entries if e.media_kind == 'video' and f'S01E{episode:02}' in e.source_path.name)
                omitted = next(e.source_path for e in plan.entries if e.media_kind == 'video' and f'S01E{episode+1:02}' in e.source_path.name)
                subtitle, font = video.with_suffix('.en.srt'), flow.source / f'Manual{episode}.zip'
                selected = next(e for e in content_choice.subtitle_edit_view(plan).entries if e.source_path == subtitle)
                plan = flow.service.apply_subtitle_override_rule(plan.task_id, PlanStore.fingerprint(plan),
                    entry_id(selected), scope=SubtitleOverrideScope.FILE, associated_video_source_path=video,
                    primary_language=language, script=None, additional_languages=additional, reason='逐文件人工修正')
                plan = change(flow.service, plan, [omitted])
                groups.append(list(map(str, (video, omitted, subtitle, font))))
                specs.append((video, omitted, subtitle, font, language, additional))
            all_paths = {r['source_path'] for r in flow.service.decision_state_store.load(plan.task_id).content_selection.to_dict()['materials']}
            rest = sorted(all_paths - {p for group in groups for p in group})
            proposed = flow.manager.preview_source_regrouping(task_id=plan.task_id,
                expected_plan_fingerprint=PlanStore.fingerprint(plan), groups=groups+[rest])
            ids = flow.manager.create_regrouped_tasks(proposed)
            await self.restart(flow)
            from src.rename.default_activation import ensure_workspace
            from src.rename.decision_state import DecisionStateStore
            for video, omitted, subtitle, font, language, additional in specs:
                with self.subTest(card=video.name):
                    child = self.selected_child(flow, ids, video)
                    current, state = await self.confirm(flow, child)
                    rows = state.content_selection.to_dict()['materials']
                    contract = next(e.subtitle for e in content_choice.subtitle_edit_view(current).entries if e.source_path == subtitle)
                    self.assertEqual((language, additional), (contract.primary_language, contract.additional_languages))
                    self.assertIn(str(omitted), state.content_selection.to_dict()['excluded_sources'])
                    self.assertIn(font,current.ignored_sources)
                    self.assertEqual(set(groups[0] if video == specs[0][0] else groups[1]), {r['source_path'] for r in rows})
                    before = (PlanStore.fingerprint(current), DecisionStateStore.fingerprint(state), len(current.subtitle_override_rules))
                    for _ in range(2):
                        state, current = ensure_workspace(flow.service, child, roots=flow.service.root_snapshot_source.observe())
                        self.assertEqual(before, (PlanStore.fingerprint(current), DecisionStateStore.fingerprint(state), len(current.subtitle_override_rules)))
                    selected = next(e for e in content_choice.subtitle_edit_view(current).entries if e.source_path == subtitle)
                    current = flow.service.apply_subtitle_override_rule(child, PlanStore.fingerprint(current),
                        entry_id(selected), scope=SubtitleOverrideScope.FILE, associated_video_source_path=video,
                        primary_language='fr', script=None, reason='拆卡后再次人工修正')
                    current = change(flow.service, current, [omitted], action='restore')
                    await self.restart(flow)
                    state, reopened = ensure_workspace(flow.service, child, roots=flow.service.root_snapshot_source.observe())
                    self.assertEqual(PlanStore.fingerprint(current), PlanStore.fingerprint(reopened))
                    contract = next(e.subtitle for e in content_choice.subtitle_edit_view(reopened).entries if e.source_path == subtitle)
                    self.assertEqual('fr', contract.primary_language)
                    self.assertNotIn(str(omitted), state.content_selection.to_dict()['excluded_sources'])

    async def test_missing_drifted_or_disconnected_birth_cannot_publish_then_corrected_can_continue(self):
        from src.rename.default_activation import ensure_workspace
        from src.rename import source_decision_transfer as transfer
        async with self.flow(automatic=False, configure_source=preview.five_seasons) as flow:
            plan, _ = self.current(flow)
            proposed = flow.manager.preview_source_regrouping(task_id=plan.task_id,
                expected_plan_fingerprint=PlanStore.fingerprint(plan))
            ids = flow.manager.create_regrouped_tasks(proposed)
            child = self.selected_child(flow, ids, self.video(plan).source_path)
            stored = flow.manager._read(child)
            # Source confirmation has its own tested UI command. This probe fixes
            # only its saved flags to isolate first-publication failure/retry.
            ready = {**stored, 'source_group_pending': False, 'queue_operation': 'rename', 'is_anime': True, 'is_movie': False}
            roots = flow.service.root_snapshot_source.observe()
            for case in ('missing', 'parent', 'scope', 'active_scope', 'derived_decisions', 'producer'):
                with self.subTest(case=case):
                    changed = json.loads(json.dumps(ready))
                    if case == 'missing':
                        changed.pop(transfer.FIELD)
                    elif case == 'parent':
                        changed[transfer.FIELD]['parent_decision_fingerprint'] = '0' * 64
                    elif case == 'scope':
                        changed[transfer.FIELD]['identities'].pop()
                    elif case == 'active_scope':
                        from src.rename.task_batch_scope import source_identity_set_fingerprint, canonical_reservation_lineage_id
                        current_scope = TaskSourceScope.from_mapping(changed['task_source_scope'])
                        sources = current_scope.source_identities[:-1]
                        source_fingerprint = source_identity_set_fingerprint(sources)
                        modified = replace(current_scope, source_identities=sources,
                            source_identity_set_fingerprint=source_fingerprint,
                            reservation_lineage_id=canonical_reservation_lineage_id(owner_task_id=child,
                                origin_batch_scope_fingerprint=current_scope.batch_scope_fingerprint,
                                source_identity_set_fingerprint=source_fingerprint))
                        changed['task_source_scope'] = modified.to_dict()
                        self.assertEqual(modified, TaskSourceScope.from_mapping(changed['task_source_scope']))
                        self.assertEqual(ready[transfer.FIELD], changed[transfer.FIELD])
                    elif case == 'derived_decisions':
                        proof = changed[transfer.FIELD]
                        path = proof['identities'][0]['source_path']
                        excluded = set(proof['excluded_sources'])
                        excluded.symmetric_difference_update({path})
                        proof['excluded_sources'] = sorted(excluded)
                        self.assertEqual(proof, transfer.SourceDecisionTransfer.from_mapping(proof).to_dict())
                    flow.manager._write(child, changed)
                    if case in ('active_scope', 'derived_decisions'):
                        # 直接验证首建共用的出生权威，避免外层活动范围门掩盖错误放宽。
                        with self.assertRaisesRegex(ValueError, '拆卡出生决定与实际父历史或子卡范围不一致'):
                            transfer.load(flow.service, child)
                    from contextlib import nullcontext
                    guard = patch.object(transfer, 'load', return_value=None) if case == 'producer' else nullcontext()
                    with guard, self.assertRaises((ValueError, OSError, RuntimeError)):
                        ensure_workspace(flow.service, child, roots=roots)
                    self.assertFalse(flow.store.path_for(child).exists())
                    self.assertFalse(flow.service.decision_state_store.path_for(child).exists())
            flow.manager._write(child, ready)
            state, current = ensure_workspace(flow.service, child, roots=roots)
            self.assertEqual(transfer.ORIGIN, state.content_selection.to_dict()['origin_kind'])
            before = PlanStore.fingerprint(current)
            with patch.object(transfer, 'load', side_effect=AssertionError('已有任务不能重放出生决定')):
                _, current = ensure_workspace(flow.service, child, roots=roots)
            self.assertEqual(before, PlanStore.fingerprint(current))
            # The ordinary authority guard refuses dropping the birth contract.
            from src.rename.inherited_subtitle_edits import require_unchanged
            with self.assertRaises(ValueError):
                require_unchanged(state.content_selection, None)
            self.assertFalse(list(flow.manager.execution_service.record_root.glob('*.json')))
