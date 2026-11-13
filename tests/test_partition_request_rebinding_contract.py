"""Shared exact-file layout and ordinary batch lock contract; no legacy split."""
from tests.source_collection_fixture import collected_identity
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from src.rename.content_policy import ContentPolicy
from src.rename.plan_store import PlanStore
from src.rename.source_basis import _proven_request, _exact_layout_root, root_for_task
from src.rename.task_batch_scope import BatchTaskSourceRequest, TaskBatchScopeManifestStore
from src.task_queue import TaskQueueManager


class PartitionRequestRebindingContracts(unittest.TestCase):
    def test_explicit_layout_is_pure_and_rejects_invalid_roots(self):
        root = Path.cwd() / 'unobserved-finite-layout'
        paths = (root / 'Video' / 'E01.mkv', root / 'Subtitle' / 'E01.srt')
        with patch.object(Path, 'is_file', side_effect=AssertionError('no live type guess')), \
                patch.object(Path, 'rglob', side_effect=AssertionError('no recursive discovery')), \
                patch.object(Path, 'stat', side_effect=AssertionError('no media read')):
            self.assertEqual(root, _exact_layout_root(paths))
            for invalid in ((), (Path('relative.mkv'),), (root / '..' / 'E01.mkv',),
                            (Path('C:/explicit/E01.mkv'), Path('D:/explicit/E01.srt'))):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    _exact_layout_root(invalid)

    def test_single_video_task_and_actual_request_share_complete_file_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = root / 'sources'
            paths = (sources / 'Video' / 'E01.mkv', sources / 'Subtitle' / 'E01.srt')
            for path in paths:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'owned exact file proof fixture')
            store = TaskBatchScopeManifestStore(root / 'task_batch_scope')
            request = BatchTaskSourceRequest('single-video', tuple(str(p) for p in paths), 'exact_file',
                tuple(collected_identity(p) for p in paths),
                content_policy=ContentPolicy.SAFE_WATCHABLE)
            reserved = store.reserve_batch(batch_id='actual-single-video', task_requests=(request,))
            queued = store.mark_tasks_enqueued(reserved.batch_id, expected_revision=reserved.revision,
                                              task_ids=('single-video',))
            active = store.activate_batch(queued.batch_id, expected_revision=queued.revision)
            scope = active.task_source_scopes[0]
            before = store.path_for(active.batch_id).read_bytes()
            layout = root_for_task({'path': str(paths[0])}, scope)
            self.assertEqual(str(sources), layout)
            service = SimpleNamespace(plan_store=PlanStore(root / 'plans'))
            _, _, proven, observation = _proven_request(service,
                batch_id=active.batch_id, scope=scope, source_root=layout)
            self.assertEqual(sources, proven)
            self.assertEqual(paths[0], observation)
            self.assertEqual(str(root), root_for_task({'path': str(root)}, scope),
                             'directory task keeps its original input, not a narrowed root')
            with self.assertRaises(ValueError):
                _proven_request(service, batch_id=active.batch_id, scope=scope, source_root=str(paths[0].parent))
            self.assertEqual(before, store.path_for(active.batch_id).read_bytes())

    def test_ordinary_batch_kernel_lock_spans_materialization_and_both_saves(self):
        from concurrent.futures import ThreadPoolExecutor
        from src.rename.task_batch_scope import TaskBatchScopeError
        with tempfile.TemporaryDirectory() as directory, ThreadPoolExecutor(max_workers=1) as contender:
            root = Path(directory)
            video = root / 'E01.mkv'
            video.write_bytes(b'owned source')
            store = TaskBatchScopeManifestStore(root / 'task_batch_scope')
            request = BatchTaskSourceRequest('ordinary', (str(video),), 'exact_file',
                (collected_identity(video),), content_policy=ContentPolicy.SAFE_WATCHABLE)
            observed = []
            def compete():
                try:
                    with store._mutation_lock('owned competing thread'):
                        return 'acquired'
                except TaskBatchScopeError:
                    return 'busy'
            def barrier(label):
                result = contender.submit(compete).result(timeout=3)
                observed.append((label, result))
                self.assertEqual('busy', result, 'OS lock released during ordinary batch publication')
            actual_materialize, actual_save = store._materialize, store._save
            def materialize(**kwargs):
                barrier('materialize')
                return actual_materialize(**kwargs)
            def save(value):
                barrier('before_' + value.state)
                actual_save(value)
                barrier('after_' + value.state)
            with patch.object(store, '_materialize', side_effect=materialize), \
                    patch.object(store, '_save', side_effect=save), \
                    patch.object(store, 'partition_active_task', side_effect=AssertionError('retired writer')):
                reserved = store.reserve_batch(batch_id='ordinary-batch', task_requests=(request,))
            self.assertEqual(['materialize', 'before_preparing', 'after_preparing',
                              'before_reservations_written', 'after_reservations_written'],
                             [r[0] for r in observed])
            self.assertEqual(reserved, store.load('ordinary-batch'))
            self.assertEqual('acquired', contender.submit(compete).result(timeout=3))
            self.assertEqual(b'owned source', video.read_bytes())

    def test_ordinary_batch_invalid_ids_duplicates_and_conflicts_do_not_save(self):
        from dataclasses import replace
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second = root / 'E01.mkv', root / 'E02.mkv'
            first.write_bytes(b'first owned source')
            second.write_bytes(b'second owned source')
            store = TaskBatchScopeManifestStore(root / 'task_batch_scope')
            def request(task_id, path):
                return BatchTaskSourceRequest(task_id, (str(path),), 'exact_file',
                    (collected_identity(path),), content_policy=ContentPolicy.SAFE_WATCHABLE)
            original = request('original-task', first)
            store.reserve_batch(batch_id='original-batch', task_requests=(original,))
            ordinary = request('new-task', second)
            cases = (
                ('../unsafe', (ordinary,)),
                ('invalid-task', (replace(ordinary, task_id='../unsafe'),)),
                ('duplicate', (ordinary, ordinary)),
                ('empty', ()),
                ('original-batch', (ordinary,)),
                ('physical-conflict', (request('foreign-task', first),)),
            )
            before = {p: p.read_bytes() for p in root.rglob('*.json')}
            for batch_id, requests in cases:
                with self.subTest(batch=batch_id), patch.object(store, '_save', wraps=store._save) as saved:
                    with self.assertRaises((ValueError, RuntimeError)):
                        store.reserve_batch(batch_id=batch_id, task_requests=requests)
                    saved.assert_not_called()
                    self.assertEqual(before, {p: p.read_bytes() for p in root.rglob('*.json')})
