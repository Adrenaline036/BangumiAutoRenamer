"""Incident-shaped GREEN contracts for ERR-20260901-014.

The fixture is a sanitized R9 S2 mixed batch.  Provider transport is frozen,
while the queue/worker/Rename/Plan/Verifier path remains production code.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from src.rename.media_classifier import LocalRole, classify_media
from src.rename.plan_review import entry_id
from src.rename.plan_store import PlanStore
from src.rename.plan_verifier import PlanVerifier
from src.rename.process import Rename
from src.rename import provider_special_evidence as provider_special
from src.rename.rename_plan import (
    AssetRole,
    PlanEntry,
    PresentationAssetPlanContract,
    RenamePlan,
)
import tests.test_complex_library_special_red_contract as special_fixture


FIXTURE_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "r24_provider_special_eligibility_r9_s2_incident.json"
)


def _fixture() -> dict[str, object]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _incident() -> dict[str, object]:
    return dict(_fixture()["incident"])


def _provider_candidate(case: dict[str, object]) -> dict[str, object]:
    provider = dict(case["provider"])
    return {
        "namespace": "bangumi",
        "entity_type": "subject",
        "provider_id": "293049",
        "title": "辉夜大小姐想让我告白？",
        "original_title": "かぐや様は告らせたい？",
        "year": 2020,
        "poster_url": special_fixture.production_fixture.POSTER_URL,
        "source_url": "https://bgm.tv/subject/293049",
        "snapshot_at": "2026-09-01T00:00:00+00:00",
        "platform": "TV",
    }


class _IncidentSearch(special_fixture._KaguyaMainSearch):
    def get_season_info(self, provider_id, season_number):
        case = _incident()
        provider = dict(case["provider"])
        if (
            int(provider_id) == int(provider["provider_id"])
            and int(season_number) == 0
        ):
            return {
                "season_number": 0,
                "episodes": tuple(
                    dict(row) for row in case["season_zero_rows"]
                ),
            }
        return None


def _configured_incident_renamer(root: Path):
    renamer = special_fixture._BASE_CONFIGURED_RENAMER(root)
    renamer.search = _IncidentSearch()
    renamer.bangumi_search = special_fixture.BangumiProviderClient(
        user_agent="BangumiAutoRenamer-R24-incident/0.1",
        session=special_fixture._KaguyaMainSession(),
        sleeper=lambda _seconds: None,
    )
    return renamer


def _all_specs(case: dict[str, object]) -> tuple[dict[str, object], ...]:
    return (
        dict(case["main"]),
        *(dict(row) for row in case["clear_extras"]),
        *(dict(row) for row in case["eligible_specials"]),
    )


class ProviderSpecialEligibilityFixtureContracts(unittest.TestCase):
    def test_fixture_is_sanitized_and_preserves_the_r9_role_shape(self):
        payload = _fixture()
        case = dict(payload["incident"])
        expected = dict(case["expected"])
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)

        self.assertEqual(
            "r24-provider-special-eligibility-red/v1",
            payload["schema_version"],
        )
        self.assertEqual(
            expected["clear_extra_count"], len(case["clear_extras"])
        )
        self.assertEqual(
            expected["eligible_special_count"], len(case["eligible_specials"])
        )
        self.assertEqual(
            expected["season_zero_candidate_rows_per_eligible_file"],
            len(case["season_zero_rows"]),
        )
        for forbidden in (
            "D:\\\\", "C:\\\\", "api_key", "cookie", ".env", "password",
        ):
            self.assertNotIn(forbidden, serialized.casefold())

        for spec in _all_specs(case):
            relative = Path(str(spec["relative_path"]))
            classification = classify_media(
                item_path=Path("source") / relative,
                candidate_name=relative.name,
                season_id=int(case["season_number"]),
                extra_context_name=relative.name,
            )
            self.assertEqual(
                LocalRole(str(spec["expected_local_role"])),
                classification.local_role,
                relative.as_posix(),
            )

    def test_incident_factory_only_replaces_provider_transport(self):
        """The fixture retains production planning and the canonical projector."""

        with tempfile.TemporaryDirectory() as directory:
            renamer = _configured_incident_renamer(Path(directory))
        self.assertIs(renamer.process.__func__, Rename.process)
        self.assertIs(
            renamer._process_traditional.__func__, Rename._process_traditional
        )
        self.assertFalse(hasattr(renamer, "_freeze_provider_special_candidates"))
        self.assertTrue(callable(provider_special.freeze_provider_special_observation))
        self.assertTrue(callable(provider_special.project_provider_special_evidence))


class ProviderSpecialEligibilityIncidentRedContracts(
    unittest.IsolatedAsyncioTestCase
):
    async def _run_real_plan(
        self,
        root: Path,
        *,
        task_id: str,
        specs: tuple[dict[str, object], ...],
        confirmed_extras: tuple[str, ...] = (),
    ):
        case = _incident()
        source = root / "sources" / "Kaguya S2"
        source_bytes = {}
        for index, spec in enumerate(specs, start=1):
            path = special_fixture._write(
                source / str(spec["relative_path"]),
                f"sanitized-r9-{index}".encode("ascii"),
            )
            source_bytes[path] = path.read_bytes()
        result = await special_fixture.ComplexLibrarySpecialRedContracts._run(
            self,
            root / "flow",
            source,
            task_id,
            "10380",
            candidate=_provider_candidate(case),
            renamer_factory=_configured_incident_renamer,
        )
        plan = result.plan_store.load(task_id)
        if confirmed_extras:
            from src.rename.plan_review import PlanReviewService
            from src.rename.initial_movie_completion import RootSnapshotSource
            from src.rename.target_routing import TargetRootSnapshot
            from tests.test_content_choice_contract import change
            flow = root / 'flow'
            roots = TargetRootSnapshot.capture({'bangumi_path': flow/'TV', 'movie_path': flow/'Movies',
                'anime_path': flow/'Anime', 'anime_movie_path': flow/'AnimeMovies'})
            service = PlanReviewService(result.plan_store, flow/'reviews', flow/'work_catalog',
                decision_state_root=flow/'decisions', task_root=flow/'tasks', record_root=flow/'records',
                provider_link_catalog_root=flow/'provider_links', root_snapshot_source=RootSnapshotSource(lambda: roots))
            for relative in confirmed_extras:
                path = source / relative
                if path in plan.ignored_sources:
                    plan = change(service, plan, [path], action='restore')
                state = service.decision_state_store.load(task_id)
                selected = next(e for e in plan.entries if e.source_path == path)
                plan = service.revise_tv_content(task_id, PlanStore.fingerprint(plan),
                    expected_revision=state.revision, mode='role', edit={'entry_id': entry_id(selected), 'role': 'extra'})
        after = {path: path.read_bytes() for path in source_bytes}
        self.assertEqual(source_bytes, after, "Plan-only RED modified source bytes")
        return source, plan

    async def test_r9_mixed_plan_preserves_typed_per_file_eligibility(self):
        """Excluded CM/Menu never gain output or official-special candidates."""

        case = _incident()
        expected = dict(case["expected"])
        with tempfile.TemporaryDirectory() as directory:
            source, plan = await self._run_real_plan(
                Path(directory),
                task_id="err014-r9-mixed",
                specs=_all_specs(case),
                confirmed_extras=('SPs/Kaguya [NCOP].mkv', 'SPs/Kaguya [NCED].mkv'),
            )
            entries = {
                entry.source_path.relative_to(source).as_posix(): entry
                for entry in plan.entries
                if entry.media_kind == "video"
            }
            clear_paths = {
                str(row["relative_path"]) for row in case["clear_extras"]
            }
            eligible_paths = {
                str(row["relative_path"]) for row in case["eligible_specials"]
                if row['expected_local_role'] == 'provider_special'
            }
            unknown_paths = {str(row['relative_path']) for row in case['eligible_specials']} - eligible_paths
            retained = {'SPs/Kaguya [NCOP].mkv', 'SPs/Kaguya [NCED].mkv'}
            excluded = clear_paths - retained
            expected_roles = {
                str(row["relative_path"]): str(row["expected_local_role"])
                for row in (
                    *(dict(item) for item in case["eligible_specials"]),
                )
                if row['expected_local_role'] == 'provider_special'
            }
            candidates = plan.metadata.get("provider_special_candidates", {})
            actual_candidate_ids = set(candidates)
            from types import SimpleNamespace
            clear_ids = {entry_id(SimpleNamespace(source_path=source / path)) for path in clear_paths}
            eligible_ids = {entry_id(entries[path]) for path in eligible_paths}
            verification = PlanVerifier().verify(plan)
            clear_extra_blockers = {
                issue.source_path.relative_to(source).as_posix()
                for issue in verification.issues
                if issue.source_path is not None
                and issue.source_path.relative_to(source).as_posix() in clear_paths
                and issue.code in {
                    "provider_special_decision_required",
                    "presentation_extra_subtype_invalid",
                }
            }
            issue_sources = {
                issue.source_path.relative_to(source).as_posix()
                for issue in verification.issues
                if issue.code == "provider_special_decision_required"
                and issue.source_path is not None
            }

            problems = []
            for path, expected_role in expected_roles.items():
                actual_role = entries[path].role
                if actual_role != expected_role:
                    problems.append(
                        f"typed presentation role {path}: "
                        f"expected {expected_role}, got {actual_role}"
                    )
            if actual_candidate_ids != eligible_ids:
                problems.append(
                    "provider-special candidates are not limited to typed "
                    f"eligible files: actual={len(actual_candidate_ids)} "
                    f"expected={len(eligible_ids)}"
                )
            if actual_candidate_ids & clear_ids:
                problems.append("clear extras were promoted by mixed-batch siblings")
            if clear_extra_blockers:
                problems.append(
                    "clear extras still receive special/subtype blockers: "
                    f"{sorted(clear_extra_blockers)}"
                )
            for identifier in eligible_ids:
                if len(tuple(candidates.get(identifier, ()))) != int(
                    expected["season_zero_candidate_rows_per_eligible_file"]
                ):
                    problems.append(
                        f"eligible special {identifier} lacks bounded frozen evidence"
                    )
            if issue_sources != eligible_paths:
                problems.append(
                    "manual issues are not limited to typed eligible files: "
                    f"actual={sorted(issue_sources)}"
                )
            self.assertTrue({source / path for path in clear_paths} <= set(plan.expected_sources))
            self.assertTrue({source / path for path in excluded} <= set(plan.ignored_sources))
            self.assertFalse(excluded & set(entries))
            for path in unknown_paths:
                self.assertEqual('unknown', entries[path].role)
                self.assertIn('ManualReview', entries[path].target_path.parts)
                self.assertTrue(entries[path].requires_review)
                self.assertNotIn(entry_id(entries[path]), candidates)
                self.assertTrue(any(issue.source_path == source / path for issue in verification.issues))
            for path in retained:
                self.assertEqual('disc_extra', entries[path].role)
                self.assertEqual(AssetRole('extra', 'other'), entries[path].presentation_asset.asset_role)
                self.assertIn('Season 02', entries[path].target_path.parts)
                self.assertIn('Extras', entries[path].target_path.parts)
            self.assertEqual([], problems, "\n".join(problems))

    async def test_world_premiere_digest_blanket_manual_contract_is_superseded(self):
        """An explicitly kept digest stays extra, not an official special."""

        case = _incident()
        digest = dict(case["legacy_conflict"])
        specs = (dict(case["main"]), digest)
        with tempfile.TemporaryDirectory() as directory:
            source, plan = await self._run_real_plan(
                Path(directory),
                task_id="err014-legacy-digest",
                specs=specs,
                confirmed_extras=(str(digest['relative_path']),),
            )
            digest_entry = next(
                entry for entry in plan.entries
                if entry.source_path.relative_to(source).as_posix()
                == str(digest["relative_path"])
            )
            identifier = entry_id(digest_entry)
            candidates = plan.metadata.get("provider_special_candidates", {})
            issue_sources = {
                issue.source_path for issue in PlanVerifier().verify(plan).issues
                if issue.code == "provider_special_decision_required"
            }
            asset = digest_entry.presentation_asset
            problems = []
            if (digest_entry.role != LocalRole.DISC_EXTRA.value or asset is None
                    or asset.asset_role != AssetRole('extra', 'other')):
                problems.append("legacy digest lost its typed DISC_EXTRA projection")
            if identifier in candidates:
                problems.append("legacy digest still receives Season 0 candidates")
            if digest_entry.source_path in issue_sources:
                problems.append("legacy digest still enters the manual tri-state gate")
            if (
                "Season 02" not in digest_entry.target_path.parts
                or "Extras" not in digest_entry.target_path.parts
            ):
                problems.append("legacy digest no longer stays in Season 02/Extras")
            self.assertEqual([], problems, "\n".join(problems))

    def test_missing_typed_role_fails_closed_without_blanket_fallback(self):
        """Disconnecting typed eligibility must not restore non-content selection."""

        case = _incident()
        expected = dict(case["expected"])
        source = Path("sanitized") / "SPs" / "Unknown Extra.mkv"
        entry = PlanEntry(
            source_path=source,
            target_path=(
                Path("sanitized-library") / "Season 02" / "Extras"
                / "Unknown Extra.mkv"
            ),
            season_id=2,
            media_kind="video",
            presentation_asset=PresentationAssetPlanContract(
                presentation_group="tv-episodes",
                asset_role=AssetRole("extra", "other"),
                segment=None,
                version_descriptor=None,
            ),
        )
        plan = RenamePlan(
            task_id="err014-disconnected-eligibility",
            source_root=Path("sanitized"),
            entries=(entry,),
            expected_sources=(source,),
        )
        from dataclasses import replace
        from src.rename.rename_plan import WorkIdentity, ProviderIdentity
        provider_id = str(dict(case['provider'])['provider_id'])
        plan = replace(plan, work_identity=WorkIdentity('confirmed','animation','Role control',2020,
            ProviderIdentity('confirmed','tmdb','tv',provider_id,'Role control',2020,'explicit_user')))
        with tempfile.TemporaryDirectory() as directory:
            renamer = _configured_incident_renamer(Path(directory))
            renamer.active_provider_selection = {
                "provider_id": str(dict(case["provider"])["provider_id"]),
            }
            observation = provider_special.freeze_provider_special_observation(
                provider_namespace="tmdb",
                provider_entity_type="tv",
                provider_id=str(dict(case["provider"])["provider_id"]),
                resolver=renamer.search.get_season_info,
                enabled=True,
            )
            projected = provider_special.project_provider_special_evidence(
                plan,
                observation,
                selected_subject_id=str(
                    dict(case["provider"])["provider_id"]
                ),
            )
        candidates = projected.metadata.get("provider_special_candidates", {})
        issue_codes = {
            issue.code for issue in PlanVerifier().verify(projected).issues
        }
        problems = []
        if candidates:
            problems.append("missing typed role fell back to blanket candidates")
        if expected["disconnect_issue_code"] not in issue_codes:
            problems.append("typed-role disconnect did not produce a fail-closed issue")
        self.assertEqual([], problems, "\n".join(problems))


if __name__ == "__main__":
    unittest.main()
