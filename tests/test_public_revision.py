"""公共修改单次生成，源文件只消费已落盘清单。"""
import unittest
from unittest.mock import patch

from src.rename.plan_store import PlanStore
from src.rename.plan_review import entry_id
from src.rename.projection_authority import validate_plan_projection_receipt
from tests.test_inventory_reuse import source_access
from tests.test_content_choice_contract import records
from tests.test_subtitle_receipt import SubtitleReceipt


class PublicRevision(unittest.IsolatedAsyncioTestCase):
    async def tv(self):
        from tests.test_initial_tv_completion_contract import InitialTVCompletionContracts
        root, service, _, result, *_ = await InitialTVCompletionContracts.fixture(self, anime=False)
        return root, service, result.plan

    async def test_tv_edit_generates_once_without_source_access(self):
        from src.rename import initial_tv_completion as tv
        root, service, plan = await self.tv()
        video = next(e for e in plan.entries if e.media_kind == 'video')
        with patch.object(tv, '_semantic', wraps=tv._semantic) as build, source_access(plan.source_root) as reads:
            result = service.revise_tv_content(plan.task_id, PlanStore.fingerprint(plan),
                expected_revision=service.decision_state_store.load(plan.task_id).revision,
                mode='role', edit={'entry_id': entry_id(video), 'role': 'extra'})
        validate_plan_projection_receipt(result)
        self.assertNotEqual(plan.projection_receipt, result.projection_receipt)
        self.assertEqual(PlanStore.fingerprint(plan), result.revision_parent_fingerprint)
        self.assertEqual('extra', service.decision_state_store.load(plan.task_id).manual_role_decisions[entry_id(video)])
        self.assertEqual((1, []), (build.call_count, reads))

    async def test_movie_edit_generates_once(self):
        from src.rename import movie_content
        root, service, plan, videos = await SubtitleReceipt.fixture(self)
        with patch.object(movie_content, 'build_movie_change', wraps=movie_content.build_movie_change) as build, \
                source_access(plan.source_root) as reads:
            intent = service.prepare_movie_version_change(plan.task_id, PlanStore.fingerprint(plan),
                groups=[dict(sources=[str(videos[0])], label='人工新标签')])
            result = service.commit_movie_version_change(intent)
        validate_plan_projection_receipt(result)
        self.assertNotEqual(plan.projection_receipt, result.projection_receipt)
        self.assertEqual(PlanStore.fingerprint(plan), result.revision_parent_fingerprint)
        groups = service.decision_state_store.load(plan.task_id).movie_version_selection.to_dict()['groups']
        self.assertEqual([str(videos[0])], groups[0]['sources'])
        self.assertEqual('人工新标签', groups[0]['label'])
        self.assertEqual((1, []), (build.call_count, reads))

    async def test_subtitle_save_projects_at_most_twice(self):
        from src.rename.decision_state import DecisionStateStore
        root, service, plan, videos = await SubtitleReceipt.fixture(self)
        selected = [entry_id(e) for e in plan.entries if e.subtitle and e.subtitle.associated_video_source_path == videos[0]]
        with patch.object(DecisionStateStore, 'project_plan', wraps=DecisionStateStore.project_plan) as project, \
                source_access(plan.source_root) as reads:
            result = service.adjust_subtitles(plan.task_id, PlanStore.fingerprint(plan), selected, main_language='ja')
        validate_plan_projection_receipt(result)
        self.assertNotEqual(plan.projection_receipt, result.projection_receipt)
        self.assertEqual(PlanStore.fingerprint(plan), result.revision_parent_fingerprint)
        self.assertEqual('ja', next(e.subtitle.primary_language for e in result.entries if entry_id(e) == selected[0]))
        self.assertEqual([], reads)
        self.assertLessEqual(project.call_count, 2)

    async def test_stale_tv_request_preserves_records(self):
        root, service, plan = await self.tv()
        before = records(root)
        with self.assertRaises((ValueError, RuntimeError)):
            service.revise_tv_content(plan.task_id, 'stale', mode='content', edit={}, expected_revision=service.decision_state_store.load(plan.task_id).revision)
        self.assertEqual(before, records(root))

    async def test_tv_scope_change_and_disconnected_input_leave_existing_records(self):
        from src.rename import initial_tv_completion as tv
        from src.utils.utils import read_task_at,write_task_at
        root,service,plan=await self.tv()
        video=next(e for e in plan.entries if e.media_kind=='video')
        def edit():
            return service.revise_tv_content(plan.task_id,PlanStore.fingerprint(plan),
                expected_revision=service.decision_state_store.load(plan.task_id).revision,
                mode='role',edit={'entry_id':entry_id(video),'role':'extra'})
        before=records(root)
        for function in ('_validate_observations','build_initial_tv_completion'):
            with self.subTest(function=function),patch.object(tv,function,return_value=None):
                with self.assertRaises((ValueError,RuntimeError)):edit()
            self.assertEqual(before,records(root))
        prepare=tv.prepare;changed=[]
        def drift(*args,**kwargs):
            intent=prepare(*args,**kwargs)
            task=read_task_at(service.task_root,plan.task_id)
            task['task_source_scope']['source_identities'][0]['size']+=1
            write_task_at(service.task_root,plan.task_id,task)
            changed.append(records(root))
            return intent
        with patch.object(tv,'prepare',side_effect=drift):
            with self.assertRaises((ValueError,RuntimeError)):edit()
        self.assertEqual(1,len(changed));self.assertEqual(changed[0],records(root))



