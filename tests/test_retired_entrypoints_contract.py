"""Old interfaces cannot become alternate writers after the N-card migration."""
import ast
import inspect
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.task_queue import TaskQueueManager
from src.rename.plan_execution import PlanExecutionError
from src.rename.plan_review import PlanReviewService, PlanReviewError
from src.rename.projection_authority import (
    ProjectionCommitService, ProjectionAuthorityError, SourceOwnershipStore,
)
from src.rename.source_partition import SourcePartitionStore, SourcePartitionError
from src.rename.task_batch_scope import TaskBatchScopeManifestStore, TaskBatchScopeError
from src.pages import plan_review_page as page


RETIRED = '旧二分拆卡.*调整计划范围'
WRITERS = (
    (SourcePartitionStore, 'save_decision', SourcePartitionError),
    (SourcePartitionStore, 'prepare', SourcePartitionError),
    (SourcePartitionStore, 'checkpoint', SourcePartitionError),
    (SourcePartitionStore, 'add_backup', SourcePartitionError),
    (SourcePartitionStore, 'complete', SourcePartitionError),
    (TaskBatchScopeManifestStore, 'partition_active_task', TaskBatchScopeError),
    (TaskBatchScopeManifestStore, 'prepare_partition_active_task', TaskBatchScopeError),
    (SourceOwnershipStore, 'partition', ProjectionAuthorityError),
    (ProjectionCommitService, 'commit_source_partition_revision', ProjectionAuthorityError),
    (PlanReviewService, 'build_source_partition_revisions', PlanReviewError),
    (PlanReviewService, 'build_source_partition_revision', PlanReviewError),
    (PlanReviewService, 'record_source_partition_revision', PlanReviewError),
)


def arguments(method):
    # An uninitialized owner proves rejection happens before reading or writing
    # anything, including creating locks or starting background workers.
    positional, keywords = [], {}
    for name, parameter in inspect.signature(method).parameters.items():
        if name == 'self' or parameter.default is not inspect.Parameter.empty:
            continue
        if parameter.kind == inspect.Parameter.KEYWORD_ONLY:
            keywords[name] = None
        else:
            positional.append(None)
    return positional, keywords


class RetiredWriters(unittest.IsolatedAsyncioTestCase):
    def test_retired_message_requires_current_safe_exit(self):
        self.assertRegex("旧二分拆卡已停用，请使用调整计划范围", RETIRED)
        self.assertNotRegex("旧二分拆卡已停用，请使用预览并调整分组", RETIRED)
        self.assertNotRegex("旧二分拆卡已停用", RETIRED)

    async def test_queue_rejects_before_start_or_any_task_lookup(self):
        owner = object.__new__(TaskQueueManager)
        for name in ('partition_task_source', 'partition_task_sources'):
            with self.subTest(name=name):
                method = getattr(TaskQueueManager, name)
                args, kwargs = arguments(method)
                with self.assertRaisesRegex(PlanExecutionError, RETIRED):
                    await method(owner, *args, **kwargs)
                body = ast.parse(textwrap.dedent(inspect.getsource(method))).body[0].body
                self.assertEqual([ast.Expr, ast.Raise], [type(node) for node in body])

    def test_production_pages_and_queue_never_call_retired_binary_apis(self):
        root = Path(__file__).resolve().parents[1] / 'src'
        retired = {name for _, name, _ in WRITERS if name not in
                   {'save_decision', 'prepare', 'checkpoint', 'add_backup', 'complete', 'partition'}}
        retired |= {'partition_task_source', 'partition_task_sources',
                    '_apply_provider_subject_boundary_inline', 'project_source_partition_revision'}
        violations = []
        for path in [root/'task_queue.py', *(root/'pages').glob('*.py')]:
            for node in ast.walk(ast.parse(path.read_text('utf-8'))):
                if isinstance(node, ast.Call):
                    name = node.func.attr if isinstance(node.func, ast.Attribute) else (
                        node.func.id if isinstance(node.func, ast.Name) else '')
                    if name in retired:
                        violations.append((path.name, node.lineno, name))
        self.assertEqual([], violations)

    def test_all_binary_mutators_are_nonwriting_tombstones(self):
        for cls, name, error in WRITERS:
            with self.subTest(name=f'{cls.__name__}.{name}'):
                method = getattr(cls, name)
                args, kwargs = arguments(method)
                with self.assertRaisesRegex(error, RETIRED):
                    method(object.__new__(cls), *args, **kwargs)
                tree = ast.parse(textwrap.dedent(inspect.getsource(method)))
                body = tree.body[0].body
                statements = [n for n in body if not (isinstance(n, ast.Expr)
                    and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str))]
                self.assertEqual([ast.Raise], [type(n) for n in statements])

    def test_old_gui_builder_removed_and_no_raw_episode_update(self):
        self.assertFalse(hasattr(page.PlanReviewDialog, '_apply_provider_subject_boundary_inline'))
        source = inspect.getsource(page.PlanReviewDialog._apply_source_episode_projection_inline)
        self.assertNotIn('decision_command_service.update', source)
        self.assertNotIn('apply_source_episode_projection(', source)
        self.assertIn('revise(', source)

    def test_private_commit_and_pure_projector_cannot_bypass_retirement(self):
        from src.rename.projection_authority import project_source_partition_revision
        self.assertNotIn('before_plan_save', inspect.getsource(ProjectionCommitService._commit_locked))
        with self.assertRaisesRegex(ProjectionAuthorityError, RETIRED):
            ProjectionCommitService._commit_locked(object.__new__(ProjectionCommitService),
                None, task_terminal=None, partition_decision=object())
        with self.assertRaisesRegex(ProjectionAuthorityError, RETIRED):
            project_source_partition_revision(None, None, None)

    def test_legacy_boundary_callback_offers_recreate_without_dispatch(self):
        owner = SimpleNamespace(plan=SimpleNamespace(metadata={}))
        with patch.object(page, 'notify') as notify:
            self.assertIsNone(page.PlanReviewDialog._dispatch_review_command(owner, {}))
        self.assertIn('安全结束', notify.call_args.args[0])

    def test_missing_receipt_is_localized_without_creating_approval(self):
        import tempfile
        from src.rename.plan_store import PlanStore
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = PlanReviewService(PlanStore(root/'plans'), root/'reviews',
                catalog_root=root/'catalog', task_root=root/'tasks',
                decision_state_root=root/'decisions')
            with patch.object(service, '_approve_entries_with_operation_lock',
                              side_effect=ProjectionAuthorityError('Plan has no ProjectionReceipt')):
                with self.assertRaisesRegex(PlanReviewError, '只读.*安全结束'):
                    service.approve_entries('old-card', 'a'*64, ())
            self.assertFalse((root/'reviews').exists())
