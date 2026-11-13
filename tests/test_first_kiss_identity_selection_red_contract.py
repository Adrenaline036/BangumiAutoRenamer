"""Incident RED for optional TMDB observations after Bangumi selection."""

from __future__ import annotations

import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from src.pages.review_operation_view import build_review_operation_presentation
from src.rename.provider_link_decision import (
    provider_subject_identity_candidates,
    resolve_provider_subject_boundary_decision,
)
from src.rename.review_operation import ProviderSelectionAwaitingUserOutcome
from src.rename.review_operation import provider_selection_command_is_capable
from tests.test_r24_phase_d_f7_f8_remediation_contract import (
    _persist_failed_attempt,
    _selected_locale_conflict_failure,
    _worker_for_failure,
)


class FirstKissSelectedIdentityRedContract(unittest.TestCase):
    def test_worker_keeps_multiple_tmdb_ids_optional_and_builds_bangumi_season_one(self):
        evidence = _selected_locale_conflict_failure()
        with tempfile.TemporaryDirectory() as directory:
            worker = _worker_for_failure(evidence, Path(directory))
            outcome = worker._resolve_bangumi_provider_selection({
                "namespace": "bangumi",
                "entity_type": "subject",
                "provider_id": "425211",
                "title": "辉夜大小姐想让我告白-初吻不会结束-",
            })
            self.assertNotIsInstance(outcome, ProviderSelectionAwaitingUserOutcome)
            name, info, resolution = outcome
            worker.search.get_tv_info_by_id.assert_not_called()
        self.assertEqual("辉夜大小姐想让我告白-初吻不会结束-", name)
        self.assertEqual(
            {"namespace": "bangumi", "entity_type": "subject", "provider_id": "425211"},
            info["_bar_primary_provider"],
        )
        self.assertEqual((), info["_bar_linked_provider_identities"])
        self.assertIsNone(resolution.tmdb_tv_id)
        self.assertEqual({"425211": 1}, dict(resolution.season_assignments))
        self.assertEqual(("425211",), tuple(resolution.work_segment["subject_ids"]))
        self.assertIn(
            "bangumi_primary_tmdb_metadata_ambiguous",
            resolution.reason_codes,
        )

    def test_persisted_animation_identity_commands_block_after_restart(self):
        evidence = _selected_locale_conflict_failure()
        with tempfile.TemporaryDirectory() as directory:
            with patch(
                "src.rename.review_operation.provider_selection_command_is_capable",
                return_value=True,
            ):
                task = _persist_failed_attempt(
                    evidence, Path(directory), awaiting_user=True
                )
        persisted_commands = tuple(task["review_operation"]["commands"])
        self.assertEqual(2, len(persisted_commands), "control did not freeze old commands")
        task["is_anime"] = True
        task["is_movie"] = False
        presentation = build_review_operation_presentation(task)
        self.assertEqual("blocked", presentation.state.value)
        self.assertEqual((), presentation.assignments)
        self.assertFalse(
            any(action.key.startswith("command-") for action in presentation.actions)
        )

        context = task["review_operation"]["context"]
        for command in persisted_commands:
            payload = command["payload"]
            args = dict(kind=command['kind'], expected_revision=command['expected_revision'],
                evidence_fingerprint=payload['evidence_fingerprint'],
                boundary_edge=payload['boundary_edge'], same_work=payload['same_work'])
            # Selectability is not a write permit; legacy UI above stays blocked.
            self.assertTrue(provider_selection_command_is_capable(task, context, **args))
            invalid = [dict(expected_revision=args['expected_revision'] + 1),
                dict(evidence_fingerprint='stale'), dict(same_work=not args['same_work'])]
            invalid.extend(dict(boundary_edge={**args['boundary_edge'], field: 'stale'})
                for field in ('candidate_fingerprint', 'selected_subject_id', 'selected_tmdb_tv_id'))
            for change in invalid:
                with self.subTest(change=change):
                    self.assertFalse(provider_selection_command_is_capable(task, context, **{**args, **change}))

    def test_general_tv_control_can_still_validate_a_frozen_tmdb_identity_command(self):
        evidence = _selected_locale_conflict_failure()
        with tempfile.TemporaryDirectory() as directory:
            with patch(
                "src.rename.review_operation.provider_selection_command_is_capable",
                return_value=True,
            ):
                task = _persist_failed_attempt(
                    evidence, Path(directory), awaiting_user=True
                )
        task["is_anime"] = False
        task["is_movie"] = False
        operation = task["review_operation"]
        context = operation["context"]
        for command in operation["commands"]:
            payload = command["payload"]
            self.assertTrue(
                provider_selection_command_is_capable(
                    task,
                    context,
                    kind=command["kind"],
                    expected_revision=command["expected_revision"],
                    evidence_fingerprint=payload["evidence_fingerprint"],
                    boundary_edge=payload["boundary_edge"],
                    same_work=payload["same_work"],
                )
            )

    def test_explicit_identity_selection_projects_tmdb_work_season_one(self):
        evidence = _selected_locale_conflict_failure()
        candidate = next(
            item for item in provider_subject_identity_candidates(evidence)
            if item["provider_id"] == "330299"
        )
        edge = {
            "boundary_kind": "selected_provider_identity",
            "selected_subject_id": "425211",
            "selected_tmdb_tv_id": "330299",
            "candidate_fingerprint": candidate["candidate_fingerprint"],
            "evidence_fingerprint": evidence["evidence_fingerprint"],
        }
        resolution, decision = resolve_provider_subject_boundary_decision(
            evidence,
            same_work=False,
            boundary_edge=edge,
        )
        self.assertIsNone(decision)
        self.assertEqual("330299", resolution.tmdb_tv_id)
        self.assertEqual(1, dict(resolution.season_assignments)["425211"])

    def test_tampered_or_unfrozen_identity_is_rejected(self):
        evidence = _selected_locale_conflict_failure()
        candidate = provider_subject_identity_candidates(evidence)[0]
        for tmdb_id in ("999999", ""):
            with self.subTest(tmdb_id=tmdb_id), self.assertRaises(ValueError):
                resolve_provider_subject_boundary_decision(
                    evidence,
                    same_work=False,
                    boundary_edge={
                        "boundary_kind": "selected_provider_identity",
                        "selected_subject_id": "425211",
                        "selected_tmdb_tv_id": tmdb_id,
                        "candidate_fingerprint": candidate[
                            "candidate_fingerprint"
                        ],
                        "evidence_fingerprint": evidence[
                            "evidence_fingerprint"
                        ],
                    },
                )


if __name__ == "__main__":
    unittest.main()
