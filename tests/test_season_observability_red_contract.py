"""Regression contracts for truthful season-authority logging.

The production path must distinguish a directory heuristic from the final
season authority selected by the persisted Bangumi/TMDB crosswalk.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tests.test_r1_real_media_production_red_contract as production_fixture
from src.rename.process import Rename
from tests.test_r24_phase_c_review_operation_migration_contract import (
    current_provider_selection_context,
)

class SeasonObservabilityRedContracts(unittest.IsolatedAsyncioTestCase):
    _NoopReviewDialog = production_fixture.R1RealQueueWorkerContracts._NoopReviewDialog
    _run_flow = production_fixture.R1RealQueueWorkerContracts._run_flow

    async def test_real_s4_provider_flow_logs_heuristic_then_final_authority(self):
        """The real worker must not present its fallback as the final season."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = production_fixture._make_season(
                root / "sources",
                "[VCB-Studio] Yama no Susume Next Summit [Ma10p_1080p]",
                "[VCB-Studio] Yama no Susume Next Summit",
            )
            with patch("src.rename.process.logger.info") as info_log:
                result = await self._run_flow(
                    root=root / "flow",
                    source=source,
                    task_id="season-observability-s4",
                    candidate=production_fixture._candidate("290980"),
                    is_anime=True,
                    is_movie=False,
                )

        messages = tuple(
            str(call.args[0])
            for call in info_log.call_args_list
            if call.args
        )
        self.assertEqual(
            "inventory_review_draft",
            result.initial_plan.metadata.get("mapping_source"),
        )
        self.assertEqual(
            "need_confirm",
            current_provider_selection_context(result.task).get(
                "previous_workflow_status"
            ),
        )
        self.assertEqual(4, result.plan.metadata.get("season_number"))
        self.assertEqual("provider_crosswalk", result.plan.metadata.get("season_source"))
        self.assertTrue(result.plan.entries)
        self.assertTrue(
            all("Season 04" in str(entry.target_path) for entry in result.plan.entries)
        )
        # Current completion consumes frozen season authority; it need not run
        # or log the retired directory-guessing branch before showing it.
        self.assertEqual(4, result.state.season_binding['season_number'])
        self.assertEqual('provider_crosswalk', result.state.season_binding['decision_source'])
        self.assertNotIn("[处理任务] 识别季号：1", messages)
        self.assertFalse(any(message.startswith("[处理任务] 最终季号：1") for message in messages))

    def test_unlinked_low_similarity_control_keeps_existing_season_one(self):
        """The observability fix must not alter legacy directory inference."""

        renamer = object.__new__(Rename)
        season = renamer.get_season_id(
            {"seasons": [{"season_number": 4, "name": "Summit"}]},
            Path("unused-organized-root"),
            Path("Main Title Completely Unrelated"),
            [{"type": "Default", "title": "Main Title"}],
        )
        self.assertEqual(1, season)


if __name__ == "__main__":
    unittest.main()
