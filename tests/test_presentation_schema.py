import hashlib
import json
import unittest
from pathlib import Path

from src.rename.plan_verifier import PlanVerifier
from src.rename.presentation import build_manual_version_descriptor
from src.rename.rename_plan import (
    AssetRole,
    EditionValue,
    EvidenceCandidate,
    EvidenceValue,
    PlanEntry,
    PlanStatus,
    PresentationAssetPlanContract,
    PresentationGroup,
    ProviderIdentity,
    RenamePlan,
    SegmentDescriptor,
    VersionDescriptor,
    SubtitlePlanContract,
    build_default_version_descriptor,
    build_version_ref,
    format_presentation_version_label,
)
from src.rename.subtitle_labels import (
    DimensionResolution,
    SUBTITLE_ASSOCIATION_RULE_VERSION,
    SUBTITLE_LABEL_RULE_VERSION,
)


def confirmed_provider(entity_type="movie", provider_id="100"):
    return ProviderIdentity(
        status="confirmed",
        namespace="tmdb",
        entity_type=entity_type,
        provider_id=provider_id,
        title="Example",
        year=2026,
        decision_source="provider_unique",
        evidence=("test fixture",),
    )


def movie_group(preferred=None):
    return PresentationGroup(
        group_id="movie-continuous",
        kind="movie_continuous",
        display_name="电影连续版",
        preferred_version_ref=preferred,
        provider_identity=confirmed_provider(),
    )


class PresentationSchemaTests(unittest.TestCase):
    def test_movie_version_ref_uses_explicit_null_season_and_episode(self):
        expected_payload = {
            "edition_key": "theatrical",
            "episode_id": None,
            "presentation_group": "movie-continuous",
            "season_id": None,
            "technical_variant_key": "1080p-sdr",
        }
        expected = "v-" + hashlib.sha256(
            json.dumps(
                expected_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:20]

        self.assertEqual(
            expected,
            build_version_ref(
                "movie-continuous", None, None, "theatrical", "1080p-sdr"
            ),
        )

    def test_default_descriptor_has_explicit_structural_default_marker(self):
        descriptor = build_default_version_descriptor(
            "tv-episodes", 1, 1
        )

        self.assertEqual("default", descriptor.edition.value.key)
        self.assertEqual("structural_default", descriptor.edition.kind)
        self.assertIsNone(descriptor.edition.selected_source)
        self.assertEqual((), descriptor.edition.candidates)
        self.assertEqual("default", descriptor.technical_variant_key)
        self.assertIsNone(descriptor.display_label)

    def test_version_label_is_deterministic_and_excludes_group_terms(self):
        self.assertEqual(
            "导演剪辑版 2160p HDR10 FLAC 5.1",
            format_presentation_version_label(
                "  导演剪辑版 ", "２１６０ｐ", "HDR10", "FLAC   5.1"
            ),
        )
        with self.assertRaises(ValueError):
            format_presentation_version_label("电影连续版", "1080p", None, None)

    def test_schema_v4_round_trip_persists_group_and_four_fields(self):
        source = Path("source/Feature.mkv")
        descriptor = build_default_version_descriptor(
            "movie-continuous", None, None
        )
        plan = RenamePlan(
            task_id="v4-roundtrip",
            source_root=Path("source"),
            entries=(PlanEntry(
                source_path=source,
                target_path=Path("target/Example.mkv"),
                media_kind="video",
                presentation_asset=PresentationAssetPlanContract(
                    presentation_group="movie-continuous",
                    asset_role=AssetRole("content_version"),
                    segment=None,
                    version_descriptor=descriptor,
                ),
            ),),
            expected_sources=(source,),
            presentation_groups=(movie_group(descriptor.version_ref),),
        )

        payload = plan.to_dict()
        restored = RenamePlan.from_dict(payload)

        self.assertEqual(7, payload["schema_version"])
        self.assertEqual(plan, restored)
        self.assertEqual(
            "movie-continuous",
            restored.entries[0].presentation_asset.presentation_group,
        )

    def test_old_v3_plan_is_rejected_instead_of_migrated(self):
        with self.assertRaisesRegex(ValueError, "expected 4"):
            RenamePlan.from_dict({"schema_version": 3})

    def test_verifier_rejects_resolved_evidence_without_source_unless_default(self):
        source = Path("source/Feature.mkv")
        edition = EvidenceValue(
            status="resolved",
            value=EditionValue("theatrical", "剧场版"),
            selected_source=None,
            candidates=(),
            manual_reason=None,
            kind="evidence",
        )
        descriptor = VersionDescriptor(
            version_ref=build_version_ref(
                "movie-continuous", None, None, "theatrical", "default"
            ),
            edition=edition,
            technical_variant_key="default",
            resolution=EvidenceValue.unknown(),
            dynamic_range=EvidenceValue.unknown(),
            key_audio=EvidenceValue.unknown(),
            display_label="剧场版",
        )
        plan = RenamePlan(
            task_id="bad-source",
            source_root=Path("source"),
            entries=(PlanEntry(
                source_path=source,
                target_path=Path("target/Example.mkv"),
                media_kind="video",
                presentation_asset=PresentationAssetPlanContract(
                    "movie-continuous",
                    AssetRole("content_version"),
                    None,
                    descriptor,
                ),
            ),),
            expected_sources=(source,),
            presentation_groups=(movie_group(descriptor.version_ref),),
        )

        result = PlanVerifier().verify(plan)

        self.assertEqual(PlanStatus.INVALID, result.status)
        self.assertIn(
            "presentation_evidence_source_missing",
            {issue.code for issue in result.issues},
        )

    def test_disc_structure_cannot_generate_mapping(self):
        source = Path("source/BDMV")
        plan = RenamePlan(
            task_id="disc",
            source_root=Path("source"),
            entries=(PlanEntry(
                source_path=source,
                target_path=Path("target/BDMV"),
                media_kind="associated",
                presentation_asset=PresentationAssetPlanContract(
                    "disc",
                    AssetRole("associated"),
                    None,
                    None,
                ),
            ),),
            expected_sources=(source,),
            presentation_groups=(PresentationGroup(
                "disc",
                "disc_structure",
                "光盘结构",
                None,
                confirmed_provider(),
            ),),
        )

        result = PlanVerifier().verify(plan)

        self.assertEqual(PlanStatus.INVALID, result.status)
        self.assertIn(
            "disc_structure_mapping_forbidden",
            {issue.code for issue in result.issues},
        )

    def test_c1_keeps_edition_and_technical_variant_as_four_unique_leaves(self):
        entries = []
        refs = set()
        for edition_key, edition_label in (
            ("theatrical", "剧场版"),
            ("directors-cut", "导演剪辑版"),
        ):
            for resolution in ("1080p", "2160p"):
                descriptor = build_manual_version_descriptor(
                    presentation_group="movie-continuous",
                    group_kind="movie_continuous",
                    season_id=None,
                    episode_id=None,
                    edition_key=edition_key,
                    edition_label=edition_label,
                    resolution=resolution,
                    dynamic_range="SDR",
                    key_audio="FLAC 2.0",
                    reason="人工确认四叶版本矩阵",
                )
                refs.add(descriptor.version_ref)
                source = Path(f"source/{edition_key}-{resolution}.mkv")
                target = Path(
                    "library/Example (2026)"
                ) / f"Example (2026) - {descriptor.display_label}.mkv"
                entries.append(PlanEntry(
                    source_path=source,
                    target_path=target,
                    media_kind="video",
                    presentation_asset=PresentationAssetPlanContract(
                        "movie-continuous",
                        AssetRole("content_version"),
                        None,
                        descriptor,
                    ),
                ))
        plan = RenamePlan(
            task_id="c1-four-leaves",
            source_root=Path("source"),
            entries=tuple(entries),
            expected_sources=tuple(entry.source_path for entry in entries),
            metadata={"target_root": "library"},
            presentation_groups=(movie_group(next(iter(refs))),),
        )

        result = PlanVerifier().verify(plan)

        # This pure shape has no admission/Decision/target authority. The four
        # leaves remain valid, but shape alone must not be an executable Plan.
        self.assertEqual(PlanStatus.READY, result.status)
        self.assertEqual(set(), {i.code for i in result.issues})
        self.assertEqual(4, len(refs))
        self.assertEqual(4, len({
            entry.presentation_asset.version_descriptor.display_label
            for entry in entries
        }))

    def test_c2_cross_group_subtitle_candidates_remain_conflict(self):
        movie_video = PlanEntry(
            Path("source/movie.mkv"),
            Path("library/Movie (2026)/Movie (2026).mkv"),
            media_kind="video",
            presentation_asset=PresentationAssetPlanContract(
                "movie-continuous", AssetRole("content_version"), None,
                build_default_version_descriptor("movie-continuous", None, None),
            ),
        )
        tv_video = PlanEntry(
            Path("source/episode.mkv"),
            Path("library/Show/Season1/Show S01E01.mkv"),
            season_id=1,
            episode_id=1,
            media_kind="video",
            presentation_asset=PresentationAssetPlanContract(
                "tv-episodes", AssetRole("content_version"), None,
                build_default_version_descriptor("tv-episodes", 1, 1),
            ),
        )
        subtitle = PlanEntry(
            Path("source/shared.SC.ass"),
            Path("library/ManualReview/shared.SC.ass"),
            media_kind="subtitle",
            requires_review=True,
            subtitle=SubtitlePlanContract(
                associated_video_source_path=None,
                association_candidates=(movie_video.source_path, tv_video.source_path),
                raw_label="SC",
                primary_language="zh",
                script="Hans",
                association_state=DimensionResolution.CONFLICT,
                parse_state=DimensionResolution.RESOLVED,
                association_rule_version=SUBTITLE_ASSOCIATION_RULE_VERSION,
                label_rule_version=SUBTITLE_LABEL_RULE_VERSION,
            ),
        )
        tv_group = PresentationGroup(
            "tv-episodes", "tv_episodes", "TV 分集版", None,
            confirmed_provider("tv", "200"),
        )
        plan = RenamePlan(
            "c2-cross-group",
            Path("source"),
            (movie_video, tv_video, subtitle),
            expected_sources=(
                movie_video.source_path, tv_video.source_path, subtitle.source_path
            ),
            presentation_groups=(movie_group(), tv_group),
        )

        result = PlanVerifier().verify(plan)

        self.assertEqual(PlanStatus.INVALID, result.status)
        self.assertIn(
            "subtitle_association_unresolved",
            {issue.code for issue in result.issues},
        )

    def test_provider_namespace_and_entity_type_must_be_registered_pair(self):
        descriptor = build_default_version_descriptor(
            "movie-continuous", None, None
        )
        bad_group = PresentationGroup(
            "movie-continuous", "movie_continuous", "电影连续版", None,
            ProviderIdentity(
                "confirmed", "tmdb", "subject", "1", "Example", 2026,
                "manual_review", ("bad pair",),
            ),
        )
        entry = PlanEntry(
            Path("source/a.mkv"), Path("library/Example/Example.mkv"),
            media_kind="video",
            presentation_asset=PresentationAssetPlanContract(
                "movie-continuous", AssetRole("content_version"), None, descriptor
            ),
        )
        result = PlanVerifier().verify(RenamePlan(
            "provider-registry", Path("source"), (entry,),
            expected_sources=(entry.source_path,), presentation_groups=(bad_group,),
        ))

        self.assertIn(
            "work_identity_provider_invalid",
            {issue.code for issue in result.issues},
        )

    def test_local_episode_layout_can_share_stable_movie_identity(self):
        descriptor = build_default_version_descriptor("tv-episodes", 1, 1)
        group = PresentationGroup(
            "tv-episodes",
            "tv_episodes",
            "TV 分集版",
            None,
            confirmed_provider("movie", "997317"),
        )
        entry = PlanEntry(
            Path("source/episode-01.mkv"),
            Path("library/Example/Season 01/Example S01E01.mkv"),
            season_id=1,
            episode_id=1,
            media_kind="video",
            presentation_asset=PresentationAssetPlanContract(
                "tv-episodes", AssetRole("content_version"), None, descriptor
            ),
        )

        result = PlanVerifier().verify(RenamePlan(
            "shared-work-identity",
            Path("source"),
            (entry,),
            expected_sources=(entry.source_path,),
            presentation_groups=(group,),
        ))

        # A movie identity does not exempt a TV-episodes layout from the current
        # contribution authority gate; no real source/Decision is supplied here.
        self.assertEqual(PlanStatus.READY, result.status)
        self.assertEqual(set(), {issue.code for issue in result.issues})
        self.assertNotIn(
            "presentation_provider_type_mismatch",
            {issue.code for issue in result.issues},
        )


if __name__ == "__main__":
    unittest.main()
