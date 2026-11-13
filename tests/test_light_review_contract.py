"""No bulk media reads for ordinary choices; component evidence only."""
from pathlib import Path
import unittest
from unittest.mock import patch
import tempfile

from nicegui.client import Client
from src.pages import plan_review_page as page
from src.rename import content_selection as content
from tests import test_initial_movie_completion_contract as completion
from tests import test_default_activation_contract as activation
from tests.source_collection_fixture import admit_task


class LightMovieChoice(unittest.IsolatedAsyncioTestCase):

    async def test_selection_and_omission_validation_do_not_read_media_contents(self):
        helper = completion.InitialMovieFullCompletionContracts('runTest')
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        _root, service, plan, primary, created = await helper.fixture(multiple=True)
        excluded = created['second']
        intent = helper.prepare(service, plan, [dict(sources=[str(primary)], label='1080p')], [excluded])
        revised = service.commit_initial_movie_completion(intent)
        choice = service.decision_state_store.load(plan.task_id).content_selection
        self.assertTrue(choice.to_dict()['evidence'])
        self.assertTrue(all(r.get('kind') == 'omission-v1' for r in choice.to_dict()['evidence']))
        self.assertIn(excluded, revised.ignored_sources)
        movie = service.decision_state_store.load(plan.task_id).movie_version_selection
        with patch.object(Path, 'open', side_effect=AssertionError('excluded contents must not be opened')):
            content.validate_omission_manifest(choice, movie_selection=movie)


class SingleWorkspace(unittest.IsolatedAsyncioTestCase):
    flow = activation.DefaultActivationProducerContracts.flow

    async def admit_auto(self, manager, source, task_id):
        await admit_task(manager, source, task_id=task_id, is_anime=None, is_movie=None)

    async def test_unresolved_normal_task_opens_the_same_review_dialog_without_initial_io(self):
        async with self.flow(admission_only=True, admit=self.admit_auto) as flow:
            class ClosedReview:
                exit_requested = True
                is_deleted = False
                def __await__(self):
                    async def closed():
                        return None
                    return closed().__await__()
                def delete(self):
                    pass
            with Client.auto_index_client.content, \
                    patch.object(page, 'RecognitionRecoveryDialog', side_effect=AssertionError('no separate first page')), \
                    patch.object(page, 'PlanReviewDialog', return_value=ClosedReview()) as review, \
                    patch('src.pages.initial_workspace.create_initial_workspace', side_effect=AssertionError('no initial source IO')):
                await page.plan_review_page(flow.task_id)
            review.assert_called_once_with(None, flow.manager._read(flow.task_id), loading=False)

    def test_movie_edit_label_is_specific_and_old_wording_removed(self):
        source = Path(page.__file__).read_text('utf-8')
        self.assertIn('电影正片版本编辑', source)
        self.assertNotIn('编辑电影版本标签与保留范围', source)

    async def test_actual_link_hydrates_same_dialog_and_card(self):
        from tests.test_initial_auto_link_contract import detail, InitialAutoLinkContracts
        helper = InitialAutoLinkContracts('runTest')
        original = page.PlanReviewDialog._load_plan_view
        seen = []
        displayed = []
        def hydrate(dialog, plan, **kwargs):
            before = (dialog.id, dialog.body.id)
            original(dialog, plan, **kwargs)
            self.assertEqual(before, (dialog.id, dialog.body.id))
            self.assertEqual(plan.task_id, dialog.plan.task_id)
            self.assertTrue(hasattr(dialog, 'entries_container'))
            seen.append(plan.task_id)
            displayed.append(dialog.retry_open_evidence)
        async with self.flow(movie=True, automatic=False, admission_only=True, admit=self.admit_auto) as flow:
            with patch.object(page.PlanReviewDialog, '_load_plan_view', hydrate):
                await helper.invoke(flow, detail(movie=True))
            self.assertEqual([flow.task_id], seen)
            from src.rename.recognition_queue_admission import prepare
            import json
            task = flow.manager._read(flow.task_id)
            request = dict(path=task['path'], is_anime=task.get('is_anime'), is_movie=task.get('is_movie'),
                custom_name=task.get('name'), custom_season_id=task.get('season_id'),
                use_ai=task.get('use_ai'), preview_only=bool(task.get('preview_only')))
            prepared = prepare(flow.manager, flow.task_id, mode='workspace', request=request,
                               opened_evidence=displayed[0])
            self.assertEqual(flow.task_id, prepared.task_id)
            task_file = flow.root/'tasks'/f'{flow.task_id}.json'
            original_task = task_file.read_bytes()
            try:
                task['error'] = 'later external task update'
                task_file.write_text(json.dumps(task), encoding='utf-8')
                with self.assertRaisesRegex(RuntimeError, '窗口打开后'):
                    prepare(flow.manager, flow.task_id, mode='workspace', request=request,
                            opened_evidence=displayed[0])
            finally:
                task_file.write_bytes(original_task)


class NoMediaComparison(unittest.TestCase):
    def test_same_named_media_is_not_hashed_to_auto_reuse(self):
        from src.rename.collision_projection import CollisionProbes, observe_path
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch('src.rename.asset_collision.fingerprint_file', side_effect=AssertionError('media hash')):
                for suffix in ('.mkv', '.eac3', '.m4a', '.mp3', '.ogg', '.tak'):
                    with self.subTest(suffix=suffix):
                        source, target = root/('source'+suffix), root/('target'+suffix)
                        source.write_bytes(b'same bytes'); target.write_bytes(b'same bytes')
                        self.assertIsNone(CollisionProbes((observe_path(source),observe_path(target))).reuse(source,target))



