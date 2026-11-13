"""来源草稿恢复及拆卡确认范围，使用真实隔离事务与固定Provider。"""
import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import unittest
from src.pages import plan_review_page as page
from src.rename.plan_store import PlanStore
from src.rename.content_associations import source_key
from tests import test_regrouping_decision_inheritance as inheritance
from tests.test_content_choice_contract import change


def sources(root, source):
    seed=next(source.glob('*S01E01*.mkv')).read_bytes()
    folder=source/'OVA';folder.mkdir()
    video=folder/'Princess Principal Crown Handler Chapter 2 [OVA].mkv'
    video.write_bytes(seed)
    for language in ('en','ja'):
        video.with_suffix('.'+language+'.srt').write_text('1\n00:00:00,000 --> 00:00:01,000\nTest\n',encoding='utf-8')
    return source


def keys(paths):
    return tuple(hashlib.sha256(source_key(p).encode('utf-8')).hexdigest()[:20] for p in paths)


class SourceContentRecovery(unittest.IsolatedAsyncioTestCase):
    flow=inheritance.RegroupingInheritance.flow
    current=inheritance.RegroupingInheritance.current
    video=inheritance.RegroupingInheritance.video
    async def confirm(self, flow, task_id):
        from src.rename.recognition_queue_admission import capture_workspace_open
        from src.pages.initial_workspace import create_initial_workspace
        opened=capture_workspace_open(flow.manager,task_id,flow.manager._read(task_id))
        await flow.manager.confirm_source_group(task_id,opened_evidence=opened)
        await asyncio.wait_for(flow.manager._queue.join(),12)
        if not flow.store.path_for(task_id).exists():
            await create_initial_workspace(task_id,flow.manager._read(task_id),media_type='anime')
        return flow.store.load(task_id),flow.service.decision_state_store.load(task_id)
    selected_child=inheritance.RegroupingInheritance.selected_child

    async def select(self, flow, task_id):
        plan=flow.store.load(task_id);state=flow.service.decision_state_store.load(task_id)
        view=SimpleNamespace(plan=plan,task_decision=state,task_data=flow.manager._read(task_id),close=Mock())
        candidate=dict(namespace='tmdb',entity_type='tv',provider_id='4242',title='普通剧集',original_title='Ordinary Show',year=2020,source_url='https://www.themoviedb.org/tv/4242',snapshot_at='2026-09-13T00:00:00Z')
        await page.PlanReviewDialog._select_provider_candidate(view,candidate,season_number=1)
        work=flow.manager._provider_reads[task_id]
        await asyncio.wait_for(work.worker,20)
        await asyncio.wait_for(flow.manager._queue.join(),12)
        return work

    async def test_failed_ova_restores_same_task_and_reselects(self):
        async with self.flow(automatic=False,configure_source=sources) as flow:
            plan,state=self.current(flow)
            ova=next(p for p in flow.source.rglob('*.mkv') if '[OVA]' in p.name)
            paths=sorted(ova.parent.iterdir())
            plan=change(flow.service,plan,paths)
            preview=flow.manager.preview_source_regrouping(task_id=plan.task_id,expected_plan_fingerprint=PlanStore.fingerprint(plan))
            ids=flow.manager.create_regrouped_tasks(preview)
            child_id=self.selected_child(flow,ids,ova)
            child,state=await self.confirm(flow,child_id)
            self.assertEqual(0,len(child.entries))
            self.assertEqual(3,len(state.content_selection.to_dict()['excluded_sources']))
            before=flow.manager._read(child_id)['source_decision_transfer']
            await self.select(flow,child_id)
            task=flow.manager._read(child_id)
            self.assertEqual('failed',task['review_operation']['context']['lifecycle'])
            self.assertIn('缺少有效视频',str(task.get('error')))
            child=flow.store.load(child_id)
            from src.rename.selection_gate import validate_selection_for_plan
            from src.rename.default_content import require_content_edit
            from src.rename import content_choice
            with self.assertRaises(ValueError):
                validate_selection_for_plan(task)
            for status in ('queued','processing','executing','completed'):
                with self.assertRaises(ValueError):
                    require_content_edit({**task,'workflow_status':status},child,state)
            with self.assertRaises(ValueError):
                require_content_edit({**task,'execution_may_have_written_targets':True},child,state)
            preview=flow.service.prepare_content_change(child_id,PlanStore.fingerprint(child),selected_entry_ids=keys(paths),action='restore',reason='原卡恢复OVA和配套')
            restored=flow.service.commit_content_change(preview)
            self.assertEqual(child_id,restored.task_id)
            self.assertEqual(set(paths),{e.source_path for e in restored.entries})
            self.assertEqual(before,flow.manager._read(child_id)['source_decision_transfer'])
            self.assertEqual('failed',flow.manager._read(child_id)['review_operation']['context']['lifecycle'])
            reopened=flow.store.load(child_id)
            self.assertEqual(PlanStore.fingerprint(restored),PlanStore.fingerprint(reopened))
            await self.select(flow,child_id)
            task=flow.manager._read(child_id)
            self.assertEqual('succeeded',task['review_operation']['context']['lifecycle'],task.get('error'))
            self.assertEqual(child_id,flow.store.load(child_id).task_id)
            self.assertTrue(flow.service.decision_state_store.load(child_id).confirmed_work)
            self.assertFalse(list(flow.manager.execution_service.record_root.glob('*.json')))

    async def test_regroup_preview_explicit_restore_is_frozen_without_parent_edit(self):
        async with self.flow(automatic=False,configure_source=sources) as flow:
            plan,state=self.current(flow)
            ova=next(p for p in flow.source.rglob('*.mkv') if '[OVA]' in p.name);paths=sorted(ova.parent.iterdir())
            plan=change(flow.service,plan,paths)
            original=PlanStore.fingerprint(plan)
            ordinary=flow.manager.preview_source_regrouping(task_id=plan.task_id,expected_plan_fingerprint=original)
            excluded={p for g in ordinary.to_dict()['groups'] for p in g['excluded_sources']}
            chosen=flow.manager.preview_source_regrouping(task_id=plan.task_id,expected_plan_fingerprint=original,excluded_sources=sorted(excluded-set(map(str,paths))))
            self.assertEqual(original,PlanStore.fingerprint(flow.store.load(plan.task_id)))
            ids=flow.manager.create_regrouped_tasks(chosen)
            child_id=self.selected_child(flow,ids,ova)
            self.assertEqual([],flow.manager._read(child_id)['source_decision_transfer']['excluded_sources'])
            child,state=await self.confirm(flow,child_id)
            self.assertEqual(set(paths),{e.source_path for e in child.entries})
            self.assertFalse(list(flow.manager.execution_service.record_root.glob('*.json')))

    async def test_regroup_preview_exclude_and_incomplete_group_rejected(self):
        async with self.flow(automatic=False,configure_source=sources) as flow:
            plan,state=self.current(flow)
            video=self.video(plan)
            fingerprint=PlanStore.fingerprint(plan)
            preview=flow.manager.preview_source_regrouping(task_id=plan.task_id,expected_plan_fingerprint=fingerprint)
            groups=[g['sources'] for g in preview.to_dict()['groups']]
            with self.assertRaises(ValueError):
                flow.manager.preview_source_regrouping(task_id=plan.task_id,expected_plan_fingerprint=fingerprint,groups=groups[:-1],excluded_sources=[])
            exclusions=sorted(set(state.content_selection.to_dict()['excluded_sources'])|{str(video.source_path)})
            chosen=flow.manager.preview_source_regrouping(task_id=plan.task_id,expected_plan_fingerprint=fingerprint,excluded_sources=exclusions)
            self.assertEqual(fingerprint,PlanStore.fingerprint(flow.store.load(plan.task_id)))
            ids=flow.manager.create_regrouped_tasks(chosen)
            child_id=self.selected_child(flow,ids,video.source_path)
            proof=flow.manager._read(child_id)['source_decision_transfer']
            self.assertIn(str(video.source_path),proof['excluded_sources'])
            self.assertNotEqual(fingerprint,proof['parent_plan_fingerprint'])
