"""Incident contracts: real temporary planning transactions; no media execution."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests.legacy_exceptional_ui_fixture import render_exceptional_episode_repair
from src.pages.plan_review_page import PlanReviewDialog
from src.rename.decision_state import DecisionCommandService
from src.rename.inventory_plan import build_inventory_review_draft
from src.rename.movie_assets import inventory_movie_assets
from src.rename.plan_review import PlanReviewError, entry_id
from tests.legacy_partition_builders import source_partition_entry_closure
from src.rename.plan_verifier import PlanVerifier
from src.rename.rename_plan import RenamePlan
from src.rename.series_catalog import SeriesCatalog
from src.rename.series_registration import manual_tv_binding
from tests.legacy_series_fixture import write_legacy_series_catalog_fixture
from src.rename.provider_special_evidence import (
    ProviderSpecialObservation, project_provider_special_evidence,
)
from src.rename.successor_authority import CohortAuthorityReader, CohortAuthorityStore, successor_authority_root
from tests.projection_test_support import save_plan_fixture
from tests.test_isolated_sample_acceptance import routing_snapshot_for
from tests.test_r24_r3_regressions_incident_red_contract import _review, _special_bundle_plan
from tests.test_r24_successor_authority_incident_red_contract import _confirmed_s1_authority


def _planning_dialog(root, *, official=False, bundle=False, catalog_tv=False):
    source = root / "source"
    source.mkdir()
    for name in ("Episode 01.mkv", "Example OVA.mkv" if official else "Unknown SP.mkv", "Other clip.mkv"):
        (source / name).write_bytes(b"temporary synthetic source")
    if bundle:
        stem = "Example OVA" if official else "Unknown SP"
        (source / f"{stem}.mka").write_bytes(b"synthetic audio")
        (source / f"{stem}.sc.ass").write_text("[Script Info]\nScriptType: v4.00+\n", encoding="utf-8")
    task = {
        "uuid": "astra-chain", "path": str(source), "name": "Example", "is_anime": not catalog_tv,
        "movie_inventory": inventory_movie_assets(source).to_dict(),
        "draft_target_root": str(root / "library"),
        "provider_identity": {"namespace": "tmdb", "entity_type": "tv", "provider_id": "100", "title": "Example", "year": 2026},
    }
    service = _review(root)
    draft = save_plan_fixture(service.plan_store, build_inventory_review_draft(task, dry_run=False))
    target = root / 'library' / ('TV' if catalog_tv else 'Anime')
    domain = 'general' if catalog_tv else 'animation'
    plan = service.complete_inventory_draft(
        draft.task_id, service.plan_store.fingerprint(draft),
        group_id="movie-continuous", group_kind="tv_episodes",
        target_root=target, reason="synthetic TV presentation",
        root_snapshot=routing_snapshot_for(target, domain=domain, group_kind="tv_episodes"),
        work_domain=domain,
    )
    rows = ({"provider_namespace": "tmdb", "provider_entity_type": "tv", "provider_id": "100", "season_number": 0, "episode_number": 6, "provider_episode_id": "100006", "title": "Official special", "air_date": "", "runtime": None},) if official else ()
    observation = ProviderSpecialObservation.build(
        state="completed_with_rows" if official else "completed_empty",
        provider_namespace="tmdb", provider_entity_type="tv", provider_id="100", episodes=rows,
    )
    plan = service._persist_revision(project_provider_special_evidence(plan, observation))
    with patch.multiple(
        "src.pages.plan_review_page", PLAN_PATH=service.plan_store.root,
        PLAN_REVIEW_PATH=root / "reviews", DECISION_STATE_PATH=service.decision_state_store.root,
        TASK_PATH=root / "task", SERIES_CATALOG_PATH=root / "series_catalog",
        WORK_CATALOG_PATH=root / "work_catalog", RECORD_PATH=root / "record",
        RECOVERY_TRANSACTION_PATH=root / "recovery_transactions",
    ):
        dialog = PlanReviewDialog(plan, task)
    dialog.plan = plan
    dialog.review_service = service
    dialog.task_decision = service.decision_state_store.load(plan.task_id)
    dialog.decision_command_service = DecisionCommandService(service.decision_state_store, service.plan_store)
    # Rendering is replaced, not the command, projector, journal or task writer.
    dialog._adopt_revision = lambda revised: setattr(dialog, "plan", revised)
    return dialog


def _special_click(dialog, root, identifier, action="unmatched_extra", episode=None):
    with patch("src.pages.plan_review_page.TASK_PATH", root / "task"), patch("src.pages.plan_review_page.notify") as notices:
        dialog._apply_exceptional_special_inline([identifier], action, episode)
    return notices


class PersistedDecisionChainTests(unittest.TestCase):
    def test_real_bundle_and_catalog_replay_preserve_all_dependencies(self):
        for official in (False, True):
            with self.subTest(official=official), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                dialog = _planning_dialog(root, official=official, bundle=True, catalog_tv=True)
                video = next(e for e in dialog.plan.entries if e.source_path.stem in {"Unknown SP", "Example OVA"} and e.media_kind == "video")
                identifier = entry_id(video)
                selected = source_partition_entry_closure(dialog.plan, [identifier])[1]
                self.assertEqual({".mkv", ".mka", ".ass"}, {e.source_path.suffix for e in selected})
                _special_click(dialog, root, identifier, "confirmed_official_special" if official else "unmatched_extra", "100006" if official else None)
                state = dialog.review_service.decision_state_store.load(dialog.plan.task_id)
                # The replay contract uses a legitimate TMDB TV/general series,
                # not a movie identity with a random Series key grafted onto it.
                series_root = root / "library" / "TV" / "Series Example (2026)"
                catalog_root = root / "series_catalog"
                catalog = SeriesCatalog(catalog_root)
                fresh = manual_tv_binding(dialog.plan.work_identity, catalog, series_root)
                existing = RenamePlan('historical-astra-series', dialog.plan.source_root, (),
                    work_identity=dialog.plan.work_identity, metadata={
                        'series_key': fresh['series_key'], 'canonical_work_root': str(series_root),
                        'season_binding_id': 'historical-season-one', 'season_number': 1,
                        'season_source': 'provider'})
                write_legacy_series_catalog_fixture(catalog, existing)
                binding = manual_tv_binding(dialog.plan.work_identity, catalog, series_root)
                state = replace(state, catalog_link_intent=None, series_binding=binding,
                    season_binding={"season_binding_id": "season-current", "season_number": 1, "decision_source": "provider"})
                replay = dialog.review_service.replan_for_catalog_drift(state, dialog.plan)
                selected_after = source_partition_entry_closure(replay, [identifier])[1]
                self.assertEqual(3, len(selected_after))
                expected_identity = (0, 6) if official else (1, None)
                self.assertTrue(all((e.season_id, e.episode_id) == expected_identity for e in selected_after))
                from src.rename.audio_projection import validate_audio
                validate_audio(replay)
                self.assertTrue(all(e.target_path.parent == selected_after[0].target_path.parent for e in selected_after))
                self.assertTrue(all(e.target_path.is_relative_to(series_root) for e in selected_after))
                self.assertFalse(any(e.target_path.is_relative_to(series_root) for e in dialog.plan.entries))
                self.assertEqual(1, replay.metadata['series_catalog_revision'])
                self.assertTrue(all(e.presentation_asset is None for e in selected_after if e.subtitle))
                other = next(e for e in dialog.plan.entries if e.source_path.name == "Other clip.mkv")
                _special_click(dialog, root, entry_id(other))
                self.assertEqual([], list(PlanVerifier().verify(dialog.plan).issues))

    def test_disconnected_projector_cannot_commit_or_clear_the_blocker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dialog = _planning_dialog(root)
            identifier = dialog.plan.metadata["provider_special_eligibility_unavailable"][0]
            before = dialog.review_service.plan_store.fingerprint(dialog.plan)
            with patch("src.rename.special_decision_projection.project_special_decisions", side_effect=ValueError("projector disconnected")):
                notices = _special_click(dialog, root, identifier)
            self.assertFalse(any(c.kwargs.get("type") == "positive" for c in notices.call_args_list))
            self.assertEqual(before, dialog.review_service.plan_store.fingerprint(dialog.review_service.plan_store.load(dialog.plan.task_id)))
            self.assertEqual({}, dict(dialog.review_service.decision_state_store.load(dialog.plan.task_id).provider_special_decisions))

    def test_extra_full_verifier_and_real_task_terminal_after_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dialog = _planning_dialog(root)
            ids = list(dialog.plan.metadata["provider_special_eligibility_unavailable"])
            self.assertEqual(2, len(ids), "producer must create the actual two unknown-video blockers")
            for identifier in ids:
                notices = _special_click(dialog, root, identifier)
                self.assertTrue(any(c.kwargs.get("type") == "positive" for c in notices.call_args_list))
            reopened = dialog.review_service.plan_store.load(dialog.plan.task_id)
            result = PlanVerifier().verify(reopened)
            self.assertEqual([], list(result.issues), "whole verifier, not a selected error-code filter")
            self.assertEqual("need_confirm", result.status.value)
            state = dialog.review_service.decision_state_store.load(reopened.task_id)
            self.assertEqual(set(ids), set(state.provider_special_decisions))
            payloads = [json.loads(p.read_text("utf-8")) for p in (root / "task").glob("*.json")]
            self.assertEqual(1, len(payloads))
            self.assertEqual("need_confirm", payloads[0]["workflow_status"])
            self.assertEqual("need_confirm", payloads[0]["plan_status"])
            self.assertEqual(dialog.review_service.plan_store.fingerprint(reopened), payloads[0]["plan_fingerprint"])
            self.assertFalse(any((root / "library").rglob("*.mkv")))

    def test_official_choice_survives_other_role_command_and_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dialog = _planning_dialog(root, official=True)
            ova = next(e for e in dialog.plan.entries if e.source_path.name == "Example OVA.mkv")
            other = next(e for e in dialog.plan.entries if e.source_path.name == "Other clip.mkv")
            self.assertTrue(dialog.plan.metadata["provider_special_candidates"].get(entry_id(ova)))
            _special_click(dialog, root, entry_id(ova), "confirmed_official_special", "100006")
            before = next(e for e in dialog.plan.entries if entry_id(e) == entry_id(ova))
            self.assertEqual((0, 6), (before.season_id, before.episode_id))
            with patch("src.pages.plan_review_page.TASK_PATH", root / "task"), patch("src.pages.plan_review_page.notify"):
                dialog._rebuild_inventory_role(entry_id(other), "extra")
            reopened = dialog.review_service.plan_store.load(dialog.plan.task_id)
            after = next(e for e in reopened.entries if entry_id(e) == entry_id(ova))
            self.assertEqual((0, 6, before.target_path), (after.season_id, after.episode_id, after.target_path))
            # A role hint is not a special classification decision for the other file.
            self.assertEqual({"provider_special_eligibility_unavailable"}, {i.code for i in PlanVerifier().verify(reopened).issues})
            _special_click(dialog, root, entry_id(other))
            reopened = dialog.review_service.plan_store.load(dialog.plan.task_id)
            self.assertEqual([], list(PlanVerifier().verify(reopened).issues))

    def test_metadata_only_extra_does_not_silence_the_blocker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dialog = _planning_dialog(root)
            identifier = dialog.plan.metadata["provider_special_eligibility_unavailable"][0]
            forged = replace(dialog.plan, metadata={**dialog.plan.metadata, "provider_special_decisions": {identifier: {"action": "unmatched_extra"}}})
            result = PlanVerifier().verify(forged)
            self.assertNotEqual("accepted", result.status.value)
            self.assertTrue(any(i.code == "provider_special_decision_projection_mismatch" for i in result.issues))

    def test_stale_revision_failure_does_not_advance_persisted_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dialog = _planning_dialog(root)
            identifier = dialog.plan.metadata["provider_special_eligibility_unavailable"][0]
            stale_plan, stale_state = dialog.plan, dialog.task_decision
            _special_click(dialog, root, identifier)
            current = dialog.review_service.plan_store.load(dialog.plan.task_id)
            dialog.plan, dialog.task_decision = stale_plan, stale_state
            notices = _special_click(dialog, root, identifier)
            self.assertFalse(any(c.kwargs.get("type") == "positive" for c in notices.call_args_list))
            self.assertEqual(dialog.review_service.plan_store.fingerprint(current), dialog.review_service.plan_store.fingerprint(dialog.review_service.plan_store.load(current.task_id)))


class BundleAndCohortTests(unittest.TestCase):
    def test_partition_includes_associated_audio(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan, identifier = _special_bundle_plan(Path(tmp), with_candidate=False)
            _, closure = source_partition_entry_closure(plan, [identifier])
            self.assertEqual(set(e.source_path for e in plan.entries), set(e.source_path for e in closure))

    def test_ambiguous_audio_is_rejected_even_if_one_video_selected(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan, identifier = _special_bundle_plan(Path(tmp), with_candidate=False)
            other = replace(plan.entries[0], source_path=plan.source_root / "different.mkv")
            ambiguous = replace(plan, entries=plan.entries + (other,))
            with self.assertRaisesRegex(PlanReviewError, "归属|ambiguous"):
                source_partition_entry_closure(ambiguous, [identifier])

    def test_unrelated_old_schema_is_isolated_but_related_remains_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            decision_root = Path(tmp) / "decisions"
            _confirmed_s1_authority(decision_root)
            store = CohortAuthorityStore(successor_authority_root(decision_root))
            record = store.list_current()[0]
            payload = record.to_dict()
            payload["schema_version"] = 1
            store.path_for(record.authority_id).write_text(json.dumps(payload), encoding="utf-8")
            reader = CohortAuthorityReader.from_decision_root(decision_root)
            self.assertEqual("no_current_authority", reader.lookup("129807", consumer_task_id="unrelated").status)
            self.assertEqual("invalid", reader.lookup(record.cohort_subject_ids[0], consumer_task_id="related").status)

    def test_unscopable_json_still_blocks_unrelated_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            decision_root = Path(tmp) / "decisions"
            _confirmed_s1_authority(decision_root)
            store = CohortAuthorityStore(successor_authority_root(decision_root))
            record = store.list_current()[0]
            store.path_for(record.authority_id).write_text("{broken", encoding="utf-8")
            self.assertEqual("invalid", CohortAuthorityReader.from_decision_root(decision_root).lookup("129807", consumer_task_id="unrelated").status)


class EventNode:
    def __init__(self, value=None, options=None):
        self.value, self.options = value, options
        self.visible = self.enabled = True
        self.handler = None
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def classes(self, *args): return self
    def props(self, *args): return self
    def set_visibility(self, value): self.visible = value
    def set_enabled(self, value): self.enabled = value
    def set_text(self, value): self.text = value
    def set_options(self, options, *, value=None): self.options, self.value = options, value
    def on_value_change(self, handler): self.handler = handler; return self
    def change(self, value):
        self.value = value
        if self.handler: self.handler(SimpleNamespace(value=value))


class EventUi:
    def __init__(self): self.selects, self.buttons = [], {}
    def label(self, *args): return EventNode()
    def row(self): return EventNode()
    def expansion(self, *args, **kwargs): return EventNode(kwargs.get('value'))
    def select(self, options, **kw):
        node = EventNode(kw.get("value"), options)
        self.selects.append(node)
        return node
    def button(self, label, **kw):
        node = EventNode()
        self.buttons[label] = node
        return node


class ExceptionalSelectionTests(unittest.TestCase):
    def test_current_file_candidates_refresh_for_both_orders(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan, known_id = _special_bundle_plan(Path(tmp), with_candidate=True)
            known = plan.entries[0]
            unknown = replace(known, source_path=known.source_path.with_name("unknown.mkv"))
            unknown_id = entry_id(unknown)
            for order in ((unknown, known), (known, unknown)):
                with self.subTest(first=entry_id(order[0])):
                    current = replace(plan, entries=order, metadata={**plan.metadata, "provider_special_eligibility_unavailable": (unknown_id,)})
                    dialog = SimpleNamespace(plan=current, snapshot=SimpleNamespace(rows=[{"entry_id": entry_id(e), "issue_codes": ("provider_special_eligibility_unavailable" if e is unknown else "provider_special_decision_required",)} for e in order]))
                    ui = EventUi()
                    render_exceptional_episode_repair(dialog, ui_api=ui, entry_identifier=entry_id, file_name=lambda p: p.name, render_partition_action=lambda cb: None)
                    files = ui.selects[0]
                    self.assertIsNotNone(files.handler)
                    files.change([known_id])
                    self.assertTrue(ui.buttons["归入特别篇"].visible)
                    self.assertEqual("123006", ui.selects[1].value)
                    files.change([unknown_id])
                    self.assertTrue(ui.buttons["归入特别篇"].visible, 'explicit user may match an unknown video to real frozen rows')
                    self.assertEqual("123006", ui.selects[1].value)
                    files.change([known_id, unknown_id])
                    self.assertFalse(ui.buttons["归入特别篇"].visible, 'one official mapping at a time')
                    files.change([known_id, unknown_id])
                    self.assertTrue(ui.buttons["保留为附加内容"].enabled)
                    self.assertFalse(ui.buttons["归入特别篇"].visible)


if __name__ == "__main__":
    unittest.main()
