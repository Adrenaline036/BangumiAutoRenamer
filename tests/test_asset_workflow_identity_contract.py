"""Stable season identity across actual recognition decisions, without Provider I/O."""
import unittest

from src.rename.projection_authority import RecognitionDecisionInput
from src.rename.rename_plan import ProviderIdentity, WorkIdentity


def recognition(task='asset-one', subject='100', season=1):
    provider = ProviderIdentity('confirmed', 'bangumi', 'subject', subject,
                                '作品', 2020, 'explicit_user')
    return RecognitionDecisionInput(task, WorkIdentity('confirmed', 'animation',
        '作品', 2020, provider), season_number=season, season_source='provider')


class AssetSeasonIdentityContracts(unittest.TestCase):
    def test_reconfirm_same_work_and_season_preserves_binding(self):
        request = recognition()
        previous = request.candidate(None)
        self.assertEqual(previous.season_binding,
                         request.candidate(previous).season_binding)

    def test_new_task_same_slot_has_same_semantic_binding(self):
        self.assertEqual(recognition().candidate(None).season_binding,
                         recognition(task='asset-two').candidate(None).season_binding)

    def test_changed_work_or_season_is_not_same_slot(self):
        previous = recognition().candidate(None)
        self.assertNotEqual(previous.season_binding['season_binding_id'],
            recognition(subject='200').candidate(previous).season_binding['season_binding_id'])
        self.assertNotEqual(previous.season_binding['season_binding_id'],
            recognition(season=2).candidate(previous).season_binding['season_binding_id'])
