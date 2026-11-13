"""Derive stable work keys from confirmed provider identity."""
import hashlib
import json
from .rename_plan import ProviderIdentity


def derive_work_intent_key(
    *,
    primary_provider: ProviderIdentity,
    group_kind: str,
) -> str:
    """Derive one stable local work key from confirmed provider authority."""

    if not isinstance(primary_provider, ProviderIdentity):
        raise TypeError("primary_provider must be ProviderIdentity")
    if primary_provider.status != "confirmed":
        raise ValueError("work intent requires a confirmed primary provider")
    namespace = str(primary_provider.namespace or "").strip().casefold()
    entity_type = str(primary_provider.entity_type or "").strip().casefold()
    provider_id = str(primary_provider.provider_id or "").strip()
    normalized_group = str(group_kind or "").strip()
    if not namespace or not entity_type or not provider_id:
        raise ValueError("work intent provider identity is incomplete")
    if normalized_group not in {"movie_continuous", "tv_episodes"}:
        raise ValueError("work intent group kind is unsupported")
    identity = json.dumps(
        {
            "entity_type": entity_type,
            "group_kind": normalized_group,
            "namespace": namespace,
            "provider_id": provider_id,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "work-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
