"""Single catalog-binding projection contract for planning and recovery."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional


@dataclass(frozen=True)
class CatalogBinding:
    backend: str
    key: str
    revision: int

    def __post_init__(self) -> None:
        if self.backend not in {"work", "series"}:
            raise ValueError("Catalog backend must be work or series")
        if not self.key:
            raise ValueError("Catalog key cannot be empty")
        if self.revision < 0:
            raise ValueError("Catalog revision cannot be negative")

    def to_dict(self) -> dict[str, object]:
        return {
            "backend": self.backend,
            "key": self.key,
            "revision": self.revision,
        }


def binding_from_decision(decision: object) -> Optional[CatalogBinding]:
    """Resolve the authoritative catalog choice from DecisionState."""

    series = getattr(decision, "series_binding", None)
    if isinstance(series, Mapping):
        key = str(series.get("series_key") or "").strip()
        if key:
            return CatalogBinding(
                "series", key, int(series.get("catalog_revision") or 0)
            )
    intent = getattr(decision, "catalog_link_intent", None)
    if isinstance(intent, Mapping):
        key = str(intent.get("work_key") or "").strip()
        if key:
            return CatalogBinding(
                "work", key, int(intent.get("catalog_revision") or 0)
            )
    return None


def binding_from_plan(plan: object) -> Optional[CatalogBinding]:
    """Read the immutable projection snapshot, with rc.34 legacy fallback."""

    metadata = getattr(plan, "metadata", {})
    if not isinstance(metadata, Mapping):
        return None
    snapshot = metadata.get("catalog_binding_snapshot")
    if isinstance(snapshot, Mapping):
        backend = str(snapshot.get("backend") or "").strip()
        key = str(snapshot.get("key") or "").strip()
        if backend in {"work", "series"} and key:
            return CatalogBinding(
                backend, key, int(snapshot.get("revision") or 0)
            )

    # Compatibility is read-only: old Plans may reach the upgrade once, but
    # every newly projected Plan writes the explicit snapshot above.
    series_key = str(metadata.get("series_key") or "").strip()
    if series_key:
        return CatalogBinding(
            "series",
            series_key,
            int(metadata.get("series_catalog_revision") or 0),
        )
    work_key = str(
        metadata.get("catalog_work_key")
        or metadata.get("proposed_work_key")
        or ""
    ).strip()
    if work_key:
        return CatalogBinding(
            "work", work_key, int(metadata.get("catalog_revision") or 0)
        )
    return None


def binding_from_failure(decision: object) -> Optional[CatalogBinding]:
    """Return the frozen binding of an unresolved structured failure."""

    failure = getattr(decision, "execution_failure", None)
    if not isinstance(failure, Mapping):
        return None
    backend = str(failure.get("catalog_backend") or "").strip()
    key = str(failure.get("catalog_key") or "").strip()
    if backend not in {"work", "series"} or not key:
        return None
    return CatalogBinding(backend, key, int(failure.get("catalog_revision") or 0))


def recovery_binding(plan: object, decision: Optional[object]) -> Optional[CatalogBinding]:
    """Route recovery by failure identity, then current authority, then Plan."""

    if decision is not None:
        failure = binding_from_failure(decision)
        if failure is not None:
            return failure
        current = binding_from_decision(decision)
        if current is not None:
            return current
    return binding_from_plan(plan)
