"""Shared schema-v5 presentation fixtures for plan tests."""

from dataclasses import replace

from src.rename.presentation import (
    build_default_presentation_contracts,
    provider_identity_from_metadata,
)
from src.rename.provider_special_evidence import ProviderSpecialObservation
from src.rename.rename_plan import RenamePlan


def with_tv_presentation(plan: RenamePlan) -> RenamePlan:
    provider = provider_identity_from_metadata(
        namespace="tmdb",
        entity_type="tv",
        provider_id="fixture-tv-1",
        title="Fixture Show",
        year=2024,
        decision_source="provider_unique",
        evidence=("test fixture",),
    )
    groups, assets = build_default_presentation_contracts(
        plan.as_mapping(),
        group_id="fixture-tv",
        group_kind="tv_episodes",
        display_name="Fixture Show",
        provider_identity=provider,
    )
    entries = tuple(
        replace(
            entry,
            presentation_asset=(
                None if entry.media_kind == "subtitle"
                else assets.get(entry.source_path)
            ),
        )
        for entry in plan.entries
    )
    metadata = dict(plan.metadata)
    metadata.setdefault(
        "provider_special_observation",
        ProviderSpecialObservation.build(
            state="completed_empty",
            provider_namespace="tmdb",
            provider_entity_type="tv",
            provider_id=provider.provider_id,
        ).to_dict(),
    )
    return replace(
        plan,
        entries=entries,
        presentation_groups=groups,
        metadata=metadata,
    )


def with_movie_presentation(plan: RenamePlan) -> RenamePlan:
    provider = provider_identity_from_metadata(
        namespace="tmdb",
        entity_type="movie",
        provider_id="fixture-movie-1",
        title="Fixture Movie",
        year=2024,
        decision_source="provider_unique",
        evidence=("test fixture",),
    )
    groups, assets = build_default_presentation_contracts(
        plan.as_mapping(),
        group_id="fixture-movie",
        group_kind="movie_continuous",
        display_name="Fixture Movie",
        provider_identity=provider,
    )
    entries = tuple(
        replace(
            entry,
            target_path=(
                entry.target_path.with_name(
                    f"{entry.target_path.parent.name}{entry.target_path.suffix}"
                )
                if entry.media_kind == "video" else entry.target_path
            ),
            presentation_asset=(
                None if entry.media_kind == "subtitle"
                else assets.get(entry.source_path)
            ),
        )
        for entry in plan.entries
    )
    return replace(plan, entries=entries, presentation_groups=groups)
