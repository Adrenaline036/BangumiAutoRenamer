"""Fail-closed ProviderLink materialization and recovery decisions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping, Optional

from .provider_link_catalog import ProviderLinkMapping


@dataclass(frozen=True)
class ProviderLinkTransition:
    mapping_status: str
    reusable_offline: bool
    task_status: str
    invoke_transfer: bool


@dataclass(frozen=True)
class ProviderLinkRecoveryPlan:
    invoke_transfer: bool
    write_series_catalog: bool
    write_provider_link_catalog: bool
    write_decision: bool
    final_mapping_status: str
    final_task_status: str


def plan_materialization_transition(
    mapping: ProviderLinkMapping,
    *,
    plan_approved: bool,
    transfer_started: bool,
    ledger_status: Optional[str],
    series_catalog_registered: bool,
    provider_link_catalog_registered: bool,
    recovery_pending: bool,
) -> ProviderLinkTransition:
    """Describe state without performing media or Catalog writes."""

    if not isinstance(mapping, ProviderLinkMapping):
        raise TypeError("mapping 必须是 ProviderLinkMapping")
    if not plan_approved:
        status = mapping.status
        return ProviderLinkTransition(status, status == "materialized_confirmed", "review_pending", False)
    if not transfer_started:
        return ProviderLinkTransition("user_confirmed", False, "approved", False)
    if ledger_status != "completed":
        return ProviderLinkTransition("user_confirmed", False, "failed", False)
    if recovery_pending or not (
        series_catalog_registered and provider_link_catalog_registered
    ):
        return ProviderLinkTransition(
            "user_confirmed", False, "catalog_update_pending", False
        )
    return ProviderLinkTransition(
        "materialized_confirmed", True, "completed", False
    )


def provider_link_blocks_execution(
    *,
    current_mapping_id: str,
    current_revision: int,
    current_fingerprint: str,
    plan_mapping_id: str,
    plan_revision: int,
    plan_fingerprint: str,
) -> bool:
    """Return true when the exact Catalog fact frozen by a Plan has drifted."""

    return (
        str(current_mapping_id) != str(plan_mapping_id)
        or int(current_revision) != int(plan_revision)
        or str(current_fingerprint) != str(plan_fingerprint)
    )


def anchor_binding_blocks_execution(
    binding: Mapping[str, object], current_mapping: ProviderLinkMapping
) -> bool:
    """Compare L1 exclusively against nested anchor identity (DS G3 P3)."""

    anchor = binding.get("anchor")
    if not isinstance(anchor, Mapping):
        return False
    try:
        return provider_link_blocks_execution(
            current_mapping_id=current_mapping.mapping_id,
            current_revision=current_mapping.revision,
            current_fingerprint=current_mapping.evidence_fingerprint,
            plan_mapping_id=str(anchor.get("mapping_id") or ""),
            plan_revision=int(anchor.get("revision", -1)),
            plan_fingerprint=str(anchor.get("evidence_fingerprint") or ""),
        )
    except (TypeError, ValueError):
        return True


def plan_provider_link_recovery(
    *,
    ledger_status: str,
    series_catalog_status: str,
    provider_link_catalog_status: str,
    decision_status: str,
    current_mapping: Mapping[str, object] | ProviderLinkMapping,
) -> ProviderLinkRecoveryPlan:
    """Plan idempotent post-ledger writes; never request a second transfer."""

    mapping = (
        current_mapping
        if isinstance(current_mapping, ProviderLinkMapping)
        else ProviderLinkMapping.from_dict(current_mapping)
    )
    if ledger_status != "completed":
        return ProviderLinkRecoveryPlan(
            False, False, False, False, mapping.status, "failed"
        )
    series_missing = series_catalog_status != "registered"
    provider_missing = provider_link_catalog_status != "registered"
    if not series_missing and not provider_missing and mapping.reusable_offline:
        return ProviderLinkRecoveryPlan(
            False, False, False, False, mapping.status, "completed"
        )
    final_status = (
        "materialized_confirmed"
        if series_catalog_status in {"registered", "missing"}
        and provider_link_catalog_status in {"registered", "missing"}
        else mapping.status
    )
    return ProviderLinkRecoveryPlan(
        invoke_transfer=False,
        write_series_catalog=series_missing,
        write_provider_link_catalog=provider_missing,
        write_decision=(
            decision_status != "healthy" or mapping.status != final_status
        ),
        final_mapping_status=final_status,
        final_task_status=(
            "completed" if final_status == "materialized_confirmed"
            else "catalog_update_pending"
        ),
    )


def apply_provider_link_recovery(
    *,
    ledger_status: str,
    series_catalog_status: str,
    provider_link_catalog_status: str,
    decision_status: str,
    current_mapping: Mapping[str, object] | ProviderLinkMapping,
    write_series_catalog: Callable[[], None],
    write_provider_link_catalog: Callable[[], None],
    write_decision: Callable[[str], None],
) -> ProviderLinkRecoveryPlan:
    """Apply only missing post-ledger facts; transfer is intentionally absent."""

    recovery = plan_provider_link_recovery(
        ledger_status=ledger_status,
        series_catalog_status=series_catalog_status,
        provider_link_catalog_status=provider_link_catalog_status,
        decision_status=decision_status,
        current_mapping=current_mapping,
    )
    if recovery.final_task_status not in {"completed", "catalog_update_pending"}:
        return recovery
    if recovery.write_series_catalog:
        write_series_catalog()
    if recovery.write_provider_link_catalog:
        write_provider_link_catalog()
    if recovery.write_decision:
        write_decision(recovery.final_mapping_status)
    return recovery
