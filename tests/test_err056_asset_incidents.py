"""Expanded live incidents, sanitized temporary fixtures only."""
from dataclasses import replace
from io import BytesIO
from pathlib import Path
import tarfile
import tempfile
import unittest
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.rename.plan_review import _infer_movie_version_descriptor
from src.rename.successor_authority import CohortAuthorityReader
from tests.test_r24_err048_cohort_coexistence_contract import material, YAMA


class ExpandedAssetIncidents(unittest.TestCase):

    def test_corrupt_or_retired_selection_cannot_claim_success(self):
        from src.rename.selection_gate import validate_selection_for_plan
        for task in ({'review_operation':{'context':{'kind':'provider_selection','lifecycle':'succeeded'}}},
                     {'selection_operation':{'state':'succeeded'}}):
            with self.subTest(task=task), self.assertRaises(ValueError):
                validate_selection_for_plan(task)

    def test_single_video_selection_failure_keeps_normal_task_card_choice_entry(self):
        from tests.test_provider_subject_plan_wiring_red_contract import _configured_renamer
        from src.rename.plan_store import PlanStore
        from tests import test_r24_tmdb_optional_animation_entry_incident_red_contract as gui
        helper = gui.TMDBOptionalAnimationEntryIncidentContract()
        with helper._persisted_workspace('single-video', is_movie=True,
                video_names=('Movie 1080p.mkv',)) as (tasks, plans, decisions, admitted):
            root = tasks.parent
            source = Path(admitted['path'])
            renamer = _configured_renamer(root, library_root=root/'library')
            renamer._provider_search_batch = Mock(return_value=SimpleNamespace(candidates=()))
            renamer._task_type_from_provider_batch = Mock(return_value='需要人工确认作品')
            with patch('src.rename.process.TASK_PATH', tasks), patch('src.rename.process.PLAN_PATH', plans):
                renamer._process(source, _is_anime=True, _is_movie=True, _tuuid='single-video')
            task = json.loads((tasks/'single-video.json').read_text('utf-8'))
            self.assertEqual(1, task['movie_inventory']['video_count'])
            self.assertEqual('inventory_only', task['plan_status'])
            opened = gui._AwaitableDialog()
            with helper._public_entry_environment(tasks, plans, decisions), \
                 patch('src.pages.plan_review_page.PlanReviewDialog', return_value=opened) as dialog, \
                 patch('src.pages.plan_review_page.RecognitionRecoveryDialog') as dead_end:
                asyncio.run(gui.TMDBOptionalAnimationEntryIncidentContract._open_from_ui_slot('single-video'))
            dead_end.assert_not_called(); dialog.assert_called_once()
            self.assertTrue(opened.awaited)
            self.assertIsNone(dialog.call_args.args[0])
            self.assertFalse(PlanStore(plans).path_for('single-video').exists())
            self.assertFalse(list(decisions.glob('*.json')))
            self.assertFalse((root/'library').exists())

    def test_selected_version_not_package_collection(self):
        root = Path('D:/Synthetic/[4K+1080p]')
        for quality in ('1080p', '2160p'):
            descriptor = _infer_movie_version_descriptor(root / f'Movie {quality}.mkv', quality, root)
            self.assertIn(quality, descriptor.display_label)

    def test_confirmed_cohort_survives_producer_task_replan_and_removal(self):
        with tempfile.TemporaryDirectory() as temporary:
            states, records = material(Path(temporary))
            for state, _record in records:
                if state == records[0][0]:
                    states.save(replace(state, confirmed_work={}, series_binding=None), expected_revision=state.revision)
                else:
                    states.path_for(state.task_id).unlink()
            result = CohortAuthorityReader.from_decision_root(states.root).lookup(YAMA[1], consumer_task_id='new-source')
            self.assertEqual('matched', result.status, result.reason_code)

