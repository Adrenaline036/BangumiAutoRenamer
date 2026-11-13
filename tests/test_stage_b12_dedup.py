"""Local repeated-work regressions; disposable sources, not real-media acceptance."""
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.rename import content_selection, source_basis, path_safety
from src.rename import initial_movie_completion as movie
from src.rename.plan_store import PlanStore
from tests.test_content_choice_contract import records


class SourceMetadataReads(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.path = self.root / 'Film.mkv'
        self.path.write_bytes(b'disposable source')
        self.observers = (lambda p: {'size': path_safety.observe_regular_file(p).st_size},)

    def test_one_leaf_lstat_per_strict_observation(self):
        original = Path.lstat
        for observe in self.observers:
            with self.subTest(observer=observe.__name__):
                expected = observe(self.path)
                calls = []
                def counted(path, *args, **kwargs):
                    calls.append(path)
                    return original(path, *args, **kwargs)
                with patch.object(Path, 'lstat', counted):
                    actual = observe(self.path)
                self.assertEqual(expected, actual)
                self.assertEqual(1, calls.count(self.path), 'checked traversal already read the leaf')
                self.assertTrue(set(self.path.parents) <= set(calls), 'parent components must still be checked')

    def test_parent_and_leaf_link_or_reparse_are_rejected(self):
        original = Path.lstat
        for observe in self.observers:
            for blocked in (self.root, self.path):
                for mode, attrs in ((stat.S_IFLNK, 0), (stat.S_IFREG, 0x400)):
                    with self.subTest(observer=observe.__name__, leaf=blocked == self.path, attrs=attrs):
                        def linked(path, *args, **kwargs):
                            if path == blocked:
                                return SimpleNamespace(st_mode=mode, st_file_attributes=attrs)
                            return original(path, *args, **kwargs)
                        with patch.object(Path, 'lstat', linked):
                            self.assertEqual(blocked, path_safety.first_reparse_component(self.path))
                            with self.assertRaises(ValueError):
                                observe(self.path)

    def test_missing_directory_and_unreadable_parent_are_not_accepted(self):
        original = Path.lstat
        for observe in self.observers:
            for path in (self.root, self.root / 'missing.mkv'):
                with self.subTest(observer=observe.__name__, path=path.name), self.assertRaises((ValueError, OSError)):
                    observe(path)
            def denied(path, *args, **kwargs):
                if path == self.root:
                    raise PermissionError('disposable unreadable parent')
                return original(path, *args, **kwargs)
            with patch.object(Path, 'lstat', denied), self.assertRaises((ValueError, OSError)):
                observe(self.path)
        self.assertIsNone(path_safety.first_reparse_component(self.root / 'missing.mkv'))
        self.assertIsNone(path_safety.first_reparse_component(Path('relative.mkv')))

    def test_separate_observations_are_fresh(self):
        for observe in self.observers:
            with self.subTest(observer=observe.__name__):
                self.path.write_bytes(b'first')
                before = observe(self.path)
                self.path.write_bytes(b'second version has another size')
                after = observe(self.path)
                self.assertNotEqual(before['size'], after['size'])
                self.path.rename(self.root/'held.mkv')
                with self.assertRaises((ValueError, OSError)):
                    observe(self.path)


class MovieCompletionWork(unittest.IsolatedAsyncioTestCase):
    async def fixture(self):
        from tests.test_initial_movie_completion_contract import InitialMovieFullCompletionContracts
        helper = InitialMovieFullCompletionContracts('runTest')
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        root, service, plan, primary, sources = await helper.fixture()
        return helper, root, service, plan, primary, sources

    async def test_prepare_builds_only_two_consumed_semantics(self):
        helper, root, service, plan, primary, _ = await self.fixture()
        before = records(root)
        with patch.object(movie, '_semantic', wraps=movie._semantic) as build:
            intent = helper.prepare(service, plan, [dict(sources=[str(primary)], label='1080p')])
        self.assertEqual(before, records(root), 'prepare must not publish business records')
        built = movie.build_initial_movie_completion(plan, service.decision_state_store.load(plan.task_id), intent)
        self.assertEqual([str(primary)], built.decision.movie_version_selection.to_dict()['groups'][0]['sources'])
        content_selection.validate_omission_manifest(built.decision.content_selection,
            movie_selection=built.decision.movie_version_selection)
        self.assertEqual(2, build.call_count, 'prepare repeats complete semantics whose output is discarded')

    async def test_commit_keeps_two_independent_builds_not_an_unused_third(self):
        helper, root, service, plan, primary, _ = await self.fixture()
        old = service.decision_state_store.load(plan.task_id)
        intent = helper.prepare(service, plan, [dict(sources=[str(primary)], label='1080p')])
        with patch.object(movie, '_semantic', wraps=movie._semantic) as build:
            completed = service.commit_initial_movie_completion(intent)
        state = service.decision_state_store.load(plan.task_id)
        self.assertEqual(old.revision + 1, state.revision)
        self.assertEqual(PlanStore.fingerprint(plan), completed.revision_parent_fingerprint)
        self.assertEqual(old.initial_source_basis, state.initial_source_basis)
        self.assertFalse(any(entry.target_path.exists() for entry in completed.entries))
        self.assertFalse(tuple(service.work_catalog.root.glob('work-*.json')))
        self.assertEqual(2, build.call_count, 'commit must retain pre-submit and independent locked reconstruction only')

    async def test_disconnected_locked_second_builder_cannot_publish(self):
        helper, root, service, plan, primary, _ = await self.fixture()
        intent = helper.prepare(service, plan, [dict(sources=[str(primary)], label='1080p')])
        before = records(root)
        actual = movie.build_initial_movie_completion
        calls = []
        def disconnected(*args):
            calls.append(True)
            return actual(*args) if len(calls) == 1 else None
        with patch.object(movie, 'build_initial_movie_completion', disconnected):
            with self.assertRaises((ValueError, RuntimeError)):
                service.commit_initial_movie_completion(intent)
        self.assertEqual(2, len(calls), 'the independent locked builder must actually be reached')
        self.assertEqual(before, records(root))

    async def test_basis_projects_the_admitted_snapshot_without_a_second_pass(self):
        from src.rename.task_batch_scope import TaskSourceScope
        helper, root, service, plan, primary, _ = await self.fixture()
        raw = service.decision_state_store.load(plan.task_id).initial_source_basis.to_dict()
        actual = source_basis._basis_payload
        def changed(**kwargs):
            value = actual(**kwargs)
            primary.write_bytes(b'different source after first observation')
            return value
        with patch.object(source_basis, '_basis_payload', changed):
            actual = source_basis.project_source_basis(task_id=plan.task_id,
                source_root=raw['source_root'], batch_id=raw['batch_id'],
                scope=TaskSourceScope.from_mapping(raw['task_source_scope']))
        self.assertEqual(raw, actual.to_dict())
