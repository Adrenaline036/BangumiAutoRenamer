"""字幕修订保留人工版本、范围与来源清单。"""
from dataclasses import replace
from contextlib import nullcontext
import unittest
from unittest.mock import patch

from src.rename.plan_store import PlanStore
from src.rename.plan_review import entry_id
from src.rename.projection_authority import validate_plan_projection_receipt
from tests.test_content_subtitle_lifecycle import apply
from tests.test_content_choice_contract import records
from tests.test_inventory_reuse import source_access


class SubtitleReceipt(unittest.IsolatedAsyncioTestCase):
    async def fixture(self, *, multi_subtitles=False):
        from tests.test_initial_movie_completion_contract import InitialMovieFullCompletionContracts, source_fixture
        helper=InitialMovieFullCompletionContracts('runTest');helper.setUp();self.addCleanup(helper.doCleanups)
        original=source_fixture._make_version
        def sources_with_subtitles(*args,**kwargs):
            source,primary,font=original(*args,**kwargs)
            primary.with_name('Movie Alternate [1080p].sc.ass').write_text('[Script Info]\nTitle: 字幕\n',encoding='utf8')
            return source,primary,font
        with patch.object(source_fixture,'_make_version',side_effect=sources_with_subtitles) if multi_subtitles else nullcontext():
            root,service,plan,primary,sources=await helper.fixture(multiple=True)
        groups=[dict(sources=[str(primary)],label='人工版本')]
        if multi_subtitles:groups.append(dict(sources=[str(sources['second'])],label='另一版本'))
        intent=helper.prepare(service,plan,groups,excluded=[] if multi_subtitles else [sources['second']])
        plan=service.commit_initial_movie_completion(intent)
        return root,service,plan,[primary,sources['second']]

    async def test_preview_and_stale_request_do_not_publish(self):
        root,service,plan,videos=await self.fixture()
        source=videos[0].with_suffix('.sc.ass')
        selected=entry_id(next(e for e in plan.entries if e.source_path==source))
        before=records(root)
        with source_access(plan.source_root) as calls:
            draft=service.adjust_subtitles(plan.task_id,PlanStore.fingerprint(plan),[selected],main_language='ja',_persist=False)
            with self.assertRaises((ValueError,RuntimeError)):
                service.adjust_subtitles(plan.task_id,'stale',[selected],main_language='ja')
        self.assertEqual([],calls);self.assertEqual(before,records(root))
        self.assertEqual('ja',next(e.subtitle.primary_language for e in draft.entries if e.source_path==source))

    async def test_actual_single_save_reaches_new_receipt(self):
        root,service,plan,videos=await self.fixture()
        result=apply(service,plan,videos[0].with_suffix('.sc.ass'))
        validate_plan_projection_receipt(result)
        self.assertNotEqual(plan.projection_receipt,result.projection_receipt)

    async def test_actual_batch_save_reaches_new_receipt(self):
        root,service,plan,videos=await self.fixture(multi_subtitles=True)
        selected=[entry_id(e) for e in plan.entries if e.subtitle and e.subtitle.associated_video_source_path in videos]
        self.assertEqual(2,len(selected))
        with source_access(plan.source_root) as calls:
            result=service.adjust_subtitles(plan.task_id,PlanStore.fingerprint(plan),selected,main_language='zh-Hans',additional_languages=['ja'])
        self.assertEqual([],calls)
        validate_plan_projection_receipt(result)
        self.assertNotEqual(plan.projection_receipt,result.projection_receipt)

    async def test_movie_save_preserves_manual_choices_without_source_access(self):
        root,service,plan,videos=await self.fixture()
        state=service.decision_state_store.load(plan.task_id)
        # 已完成任务记录属于另一任务，字幕修订不能影响它。
        archive=root/'records'/'completed-unrelated.json';archive.parent.mkdir(exist_ok=True)
        archive.write_text('{"workflow_status":"completed","entry":"unchanged"}',encoding='utf8')
        protected=archive.read_bytes()
        with source_access(plan.source_root) as calls:
            result=apply(service,plan,videos[0].with_suffix('.sc.ass'))
            rule=result.subtitle_override_rules[-1].rule_id
            result=service.revert_subtitle_rule(plan.task_id,PlanStore.fingerprint(result),rule,'撤销本次字幕调整')
        self.assertEqual([],calls)
        saved=service.decision_state_store.load(plan.task_id)
        self.assertEqual(state.movie_version_selection,saved.movie_version_selection)
        self.assertEqual(plan.ignored_sources,result.ignored_sources)
        self.assertEqual(protected,archive.read_bytes())
        self.assertFalse(service.load_or_create(result).approved_entry_ids)
        validate_plan_projection_receipt(result)


    async def test_disconnected_validator_and_video_change_cannot_publish(self):
        from src.rename.projection_authority import ProjectionCommitService
        root,service,plan,videos=await self.fixture()
        selected=entry_id(next(e for e in plan.entries if e.subtitle and e.subtitle.associated_video_source_path==videos[0]))
        before=records(root)
        with patch('src.rename.content_choice.validate_subtitle_contracts',return_value=None) as guard:
            with self.assertRaises((ValueError,RuntimeError)):
                service.adjust_subtitles(plan.task_id,PlanStore.fingerprint(plan),[selected],main_language='ja')
        guard.assert_called();self.assertEqual(before,records(root))
        original=ProjectionCommitService._commit_locked
        reached=[]
        def changed(transaction,envelope,**kwargs):
            reached.append(True)
            draft=envelope.draft_plan
            draft=replace(draft,entries=tuple(replace(e,episode_id=99) if e.media_kind=='video' else e for e in draft.entries))
            return original(transaction,replace(envelope,draft_plan=draft),**kwargs)
        with patch.object(ProjectionCommitService,'_commit_locked',changed):
            with self.assertRaises((ValueError,RuntimeError)):
                service.adjust_subtitles(plan.task_id,PlanStore.fingerprint(plan),[selected],main_language='ja')
        self.assertTrue(reached);self.assertEqual(before,records(root))

    async def test_interrupted_subtitle_commit_keeps_original_then_retries(self):
        root,service,plan,videos=await self.fixture()
        before={k:v for k,v in records(root).items() if not k.startswith('projection_journal/')}
        reached=[]
        def stop(_service,stage):
            if stage=='decision_written':
                reached.append(stage);raise RuntimeError('字幕事务中断')
        with patch('src.rename.projection_authority.ProjectionCommitService._checkpoint',stop):
            with self.assertRaisesRegex(RuntimeError,'字幕事务中断'):
                apply(service,plan,videos[0].with_suffix('.sc.ass'))
        self.assertEqual(['decision_written'],reached)
        after={k:v for k,v in records(root).items() if not k.startswith('projection_journal/')}
        self.assertEqual(before,after)
        revised=apply(service,plan,videos[0].with_suffix('.sc.ass'))
        validate_plan_projection_receipt(revised)

    async def test_actual_editor_save_feedback_remains_open_and_reloads(self):
        from tests.test_content_workbench_gui import ContentWorkbenchGui
        root,service,plan,videos=await self.fixture()
        holder=ContentWorkbenchGui('runTest')
        holder.create_page(prepared=(root,service,plan,videos),cleanup_owner=self)
        await holder.driver.click('调整字幕结构')
        editor=holder.view._subtitle_editor
        selected=[key for key,e in editor.entries.items() if e.subtitle.associated_video_source_path==videos[0]]
        self.assertTrue(selected);editor.set_selection(selected);editor.main.value='zh-Hans'
        editor.has_additional.value=True;editor.additional.value=['ja']
        with source_access(plan.source_root) as calls:
            await holder.driver.click_element(editor.save_button)
        self.assertEqual([],calls)
        self.assertEqual('所选字幕调整已保存',editor.feedback.text)
        self.assertTrue(editor.dialog.value)
        editor.dialog.close();await holder.driver.click('调整字幕结构')
        self.assertEqual(PlanStore.fingerprint(editor.plan),PlanStore.fingerprint(holder.view._subtitle_editor.plan))

