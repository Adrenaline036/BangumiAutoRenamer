"""Four scoped repairs: canonical archive projection and actual movie form."""
from pathlib import Path
import tempfile
import unittest





class MovieFirstFormContracts(unittest.IsolatedAsyncioTestCase):
    async def test_initial_same_label_requires_explicit_replacement_or_distinct_append(self):
        from tests import test_err059_delivery_incidents as fixture
        from src.pages.asset_choice_forms import INITIAL_MOVIE_REPLACEMENT_HELP
        import shutil
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            harness, initial, primary = await fixture.DeliveryIncidents('runTest').movie(root)
            completed = await harness._complete_movie(root=root, result=initial, primary=primary)
            task, _ledger, _service = await harness._approve_and_execute(root=root, result=completed)
            self.assertEqual('completed', task['execution_status'])
            source = root/'incoming'; source.mkdir()
            video = source/primary[0].name
            shutil.copyfile(primary[0], video)
            incoming = await harness._run_provider_flow(root, source, 'same-label')
            rejected = await harness._complete_movie(root=root, result=incoming, primary=video)
            self.assertEqual('inventory_review_draft', rejected.plan.metadata['mapping_source'])
            self.assertIn(INITIAL_MOVIE_REPLACEMENT_HELP, rejected.form_messages)
            self.assertEqual(((True, ''),), rejected.replacement_controls)
            self.assertFalse(rejected.review.load_or_create(rejected.plan).approved_entry_ids)
            distinct = await harness._complete_movie(root=root, result=rejected, primary=video,
                version_groups=[dict(sources=[str(video)], label='1080p-待换源')])
            self.assertIsNotNone(distinct.state.movie_version_selection)
            self.assertFalse(distinct.review.load_or_create(distinct.plan).approved_entry_ids)
