"""Episode display titles are search observations, never work/type authority."""
import unittest
import json

from src.rename.cleaner import parse_release_title
from tests.source_collection_fixture import admit_task


class EpisodeSearchTitleContracts(unittest.TestCase):
    def test_delimited_episode_title_yields_series_and_year_without_losing_literal(self):
        original = 'Ordinary Show (2020) - S01E01 - Pilot (1080p BluRay x265 Group).mkv'
        parsed = parse_release_title(original)
        self.assertEqual(('Ordinary Show', 2020), parsed.primary.key)
        self.assertEqual(original, parsed.original)
        self.assertIn(('Ordinary Show 2020 - S01E01 - Pilot', 0),
            [candidate.key for candidate in parsed.candidates])
        self.assertIn(('Ordinary Show', 0), [candidate.key for candidate in
            parse_release_title('Ordinary Show - S02E03 - The Beginning.mkv').candidates])

    def test_title_numbers_and_unstructured_episode_words_are_preserved(self):
        for title in ('Studio S01E01 Diary', 'Steins;Gate - Zero',
                      'Space 1999 Remastered', '1917', 'S01E01 - Pilot'):
            with self.subTest(title=title):
                self.assertEqual((title, 0), parse_release_title(title + '.mkv').primary.key)


class EpisodeTitleAutomaticFlowContracts(unittest.IsolatedAsyncioTestCase):
    async def test_exact_file_without_custom_name_or_type_reaches_unapproved_tv(self):
        from tests import test_default_activation_contract as activation
        def source_file(root, source):
            current = next(source.glob('*S01E01*.mkv'))
            target = current.with_name('Ordinary Show (2020) - S01E01 - Pilot [1080p].mkv')
            current.rename(target)  # exact file created by this disposable fixture
            return target
        async def admit(manager, source, task_id):
            self.original_bytes = source.read_bytes()
            await admit_task(manager, source, task_id=task_id, is_anime=None, is_movie=None,
                custom_name=None)
        observed = []
        def configure(worker):
            worker.bangumi_search = None
            old = worker.search.get_tv_info
            def query(name, year=0):
                observed.append((name, year))
                return old(name, year) if (name, year) == ('Ordinary Show', 2020) else ('', None)
            worker.search.get_tv_info = query
        async with activation.DefaultActivationProducerContracts().flow(
                admit=admit, configure_source=source_file, configure_worker=configure) as flow:
            task = flow.manager._read(flow.task_id)
            self.assertIn(('Ordinary Show', 2020), observed)
            self.assertTrue(flow.store.path_for(flow.task_id).exists(), task.get('error'))
            state = flow.service.decision_state_store.load(flow.task_id)
            self.assertTrue(state.confirmed_work)
            self.assertEqual('tv', state.confirmed_work['primary_provider']['entity_type'])
            self.assertEqual('need_confirm', task['workflow_status'])
            self.assertIsNone(task['automatic_recognition_origin']['custom_name'])
            self.assertIsNone(task['automatic_recognition_origin']['is_movie'])
            self.assertTrue(all(not e.target_path.exists() for e in flow.store.load(flow.task_id).entries))
            self.assertEqual(self.original_bytes, flow.source.read_bytes())
            self.assertFalse(list(flow.manager.execution_service.record_root.glob('*.json')))
            for review in flow.manager.review_root.glob('*.json'):
                self.assertFalse(json.loads(review.read_text(encoding='utf-8')).get('approved_entry_ids'))

    async def test_no_match_prefix_still_queries_original_literal_within_search_limit(self):
        from tests import test_default_activation_contract as activation
        seen = []
        literal = 'Ordinary Show 2020 - S01E01 - Pilot'
        def configure(worker):
            worker.bangumi_search = None
            for kind in ('tv', 'movie'):
                def no_match(name, year=0, _kind=kind):
                    seen.append((_kind, name, year))
                    return '', None
                setattr(worker.search, 'get_' + kind + '_info', no_match)
        async def admit(manager, source, task_id):
            await admit_task(manager, source, task_id=task_id, is_anime=None, is_movie=None,
                custom_name=literal)
        async with activation.DefaultActivationProducerContracts().flow(
                admit=admit, configure_worker=configure) as flow:
            for kind in ('tv', 'movie'):
                self.assertIn((kind, 'Ordinary Show', 2020), seen)
                self.assertIn((kind, literal, 0), seen)
            self.assertFalse(flow.store.path_for(flow.task_id).exists())
            self.assertEqual('need_confirm', flow.manager._read(flow.task_id)['workflow_status'])
