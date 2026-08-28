"""Test-only helpers for constructing a current, receipted Plan fixture.

Production code must obtain this receipt from ``FinalPlanProjector``. Older
unit tests often need only an already-persisted Plan as their starting state;
this helper makes that precondition explicit without reopening a production
write bypass.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from src.rename.decision_state import DecisionState, DecisionStateStore
from src.rename.plan_store import PlanStore
from src.rename.projection_authority import (
    commit_projected_plan,
)
from src.rename.rename_plan import RenamePlan


def _plain_fixture_value(value: object) -> object:
    """Thaw immutable Plan metadata before persisting a test Decision."""

    if isinstance(value, Mapping):
        return {
            str(key): _plain_fixture_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_plain_fixture_value(item) for item in value]
    return value


def _materialize_fixture_sources(plan: RenamePlan) -> RenamePlan:
    """Give legacy synthetic plans the real-file precondition of projection.

    Production projection must fail closed for a missing source. Older tests
    that start from an already-materialized Plan historically used path-only
    fixtures, so the test boundary (not production) owns creating those
    synthetic bytes before it invokes the canonical projector.
    """

    expected_sources = tuple(Path(item) for item in plan.expected_sources)
    if not expected_sources and plan.entries:
        expected_sources = tuple(entry.source_path for entry in plan.entries)
        plan = replace(plan, expected_sources=expected_sources)
    for source in expected_sources:
        if source.exists():
            continue
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"synthetic projection fixture source\n")
    return plan


def activate_batch_fixture(store, manifest):
    """Advance a synthetic manifest through the real durable admission gates."""

    enqueued = store.mark_tasks_enqueued(
        manifest.batch_id,
        expected_revision=manifest.revision,
        task_ids=tuple(request.task_id for request in manifest.requests),
    )
    return store.activate_batch(
        enqueued.batch_id,
        expected_revision=enqueued.revision,
    )


def reserve_active_batch_fixture(store, *, batch_id, task_requests):
    """Test-only convenience without reopening production auto-activation."""

    manifest = store.reserve_batch(
        batch_id=batch_id,
        task_requests=task_requests,
    )
    return activate_batch_fixture(store, manifest)


def save_plan_fixture(
    store: PlanStore,
    plan: RenamePlan,
    *,
    state_store: DecisionStateStore | None = None,
) -> RenamePlan:
    """Persist a fixture through the same typed projector used in production."""

    plan = _materialize_fixture_sources(plan)
    state_store = state_store or DecisionStateStore(
        store.root.parent / "decision_state"
    )
    previous = state_store.load_optional(plan.task_id)
    candidate = previous or DecisionState(task_id=plan.task_id)
    metadata = dict(plan.metadata)
    if plan.work_identity is not None and plan.work_identity.status == "confirmed":
        primary = plan.work_identity.primary_provider.to_dict()
        linked = tuple(
            item.to_dict()
            for item in plan.linked_provider_identities
            if item.status == "confirmed"
        )
        candidate = replace(
            candidate,
            confirmed_work=plan.work_identity.to_dict(),
            provider_evidence=(primary,) + linked,
            linked_provider_identities=linked,
            library_domain=(
                plan.work_identity.domain
                if plan.work_identity.domain in {"animation", "general"}
                else None
            ),
            layout_mode=(
                {
                    "movie_continuous": "movie_versions",
                    "tv_episodes": "tv_episodes",
                }.get(plan.presentation_groups[0].kind)
                if plan.presentation_groups else None
            ),
        )
    fixture_changes: dict[str, object] = {}
    series_key = str(metadata.get("series_key") or "").strip()
    if series_key:
        series_revision = metadata.get("series_catalog_revision", 0)
        fixture_changes["series_binding"] = {
            "series_key": series_key,
            "canonical_root": str(
                metadata.get("canonical_work_root")
                or metadata.get("target_root")
                or ""
            ),
            "catalog_revision": (
                series_revision
                if isinstance(series_revision, int)
                and not isinstance(series_revision, bool)
                and series_revision >= 0
                else 0
            ),
            "decision_source": "test_fixture",
        }
    season_number = metadata.get("season_number", metadata.get("season_id"))
    season_binding_id = str(metadata.get("season_binding_id") or "").strip()
    if (
        season_binding_id
        and isinstance(season_number, int)
        and not isinstance(season_number, bool)
    ):
        fixture_changes["season_binding"] = {
            "season_binding_id": season_binding_id,
            "season_number": season_number,
            "decision_source": str(
                metadata.get("season_source") or "test_fixture"
            ),
        }
    provider_link = metadata.get("provider_link_snapshot")
    if isinstance(provider_link, Mapping):
        fixture_changes["provider_link_binding"] = _plain_fixture_value(
            provider_link
        )
    work_key = str(
        metadata.get("catalog_work_key")
        or metadata.get("proposed_work_key")
        or ""
    ).strip()
    if work_key and not series_key:
        catalog_revision = metadata.get("catalog_revision", 0)
        fixture_changes["catalog_link_intent"] = {
            "work_key": work_key,
            "catalog_revision": (
                catalog_revision
                if isinstance(catalog_revision, int)
                and not isinstance(catalog_revision, bool)
                and catalog_revision >= 0
                else 0
            ),
        }
    if fixture_changes:
        candidate = replace(candidate, **fixture_changes)
    _decision, projected = commit_projected_plan(
        state_store,
        store,
        previous=previous,
        candidate=candidate,
        draft_plan=plan,
    )
    return projected
