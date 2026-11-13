"""First explicit manual movie confirmation must use the typed projector."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.rename.plan_review import entry_id
from tests import test_presentation_review as fixture


class ManualMovieFirstSelection(unittest.TestCase):
    def test_disconnected_first_selection_cannot_persist_partial_authority(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            service, store, plan = fixture.PresentationReviewTests('runTest')._service_and_plan(root)
            self.assertNotIn('movie_version_selection', plan.metadata)
            previous = store.path_for(plan.task_id).read_bytes()
            state = service.decision_state_store.load(plan.task_id)
            with patch('src.rename.movie_version_selection.project_selection', side_effect=ValueError('disconnected')), \
                    self.assertRaisesRegex(ValueError, 'disconnected'):
                service.revise_presentation_asset(plan.task_id, store.fingerprint(plan), entry_id(plan.entries[0]),
                    presentation_group='fixture-movie', role_kind='content_version',
                    edition_key='theatrical', edition_label='院线版', resolution='1080p',
                    dynamic_range='SDR', key_audio='FLAC 2.0', preferred=True, reason='首次显式版本确认')
            self.assertEqual(previous, store.path_for(plan.task_id).read_bytes())
            self.assertEqual(state, service.decision_state_store.load(plan.task_id))
            self.assertFalse(any(entry.target_path.exists() for entry in plan.entries))


if __name__ == '__main__':
    unittest.main()
