"""Stable work intent and pending work-scoped font authority.

The display title, source package and target path are deliberately excluded
from work identity.  A pending record changes revision only when its content
set or explicit primary decision changes, so equal-content sibling Plans can
be approved together while different-content siblings become stale/blocked.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Iterable, Mapping, Optional
import uuid

from .file_lock import FileLockBusyError, advisory_file_lock
from .rename_plan import ProviderIdentity


WORK_INTENT_POLICY_VERSION = "work-intent-v1"
WORK_FONT_INTENT_SCHEMA_VERSION = 1
WORK_FONT_CONDITIONAL_ACTION = "create_or_reuse_registered_identical"
WORK_FONT_APPEND_POLICY = "completed-sibling-append-v1"

_WORK_KEY = re.compile(r"^work-[0-9a-f]{32}$")
_CONTENT_KEY = re.compile(r"^sha256:(?P<size>\d+):(?P<digest>[0-9a-f]{64})$")


class WorkIntentError(RuntimeError):
    pass


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


def make_content_key(*, size: int, digest: str) -> str:
    normalized_digest = str(digest or "").strip().casefold()
    if (
        not isinstance(size, int)
        or isinstance(size, bool)
        or size < 0
        or re.fullmatch(r"[0-9a-f]{64}", normalized_digest) is None
    ):
        raise WorkIntentError("work font content identity is invalid")
    return f"sha256:{size}:{normalized_digest}"


def parse_content_key(value: object) -> tuple[int, str]:
    match = _CONTENT_KEY.fullmatch(str(value or "").strip().casefold())
    if match is None:
        raise WorkIntentError("work font content key is invalid")
    return int(match.group("size")), match.group("digest")


@dataclass(frozen=True)
class WorkIntentSnapshot:
    work_key: str
    content_revision: int
    namespace: str
    entity_type: str
    provider_id: str
    group_kind: str
    content_keys: tuple[str, ...]
    primary_content_key: Optional[str]
    primary_decision_source: Optional[str]
    snapshot_fingerprint: str

    def to_plan_contract(
        self,
        *,
        store_root: Path,
        assets: Iterable[Mapping[str, object]],
    ) -> dict[str, object]:
        return {
            "schema_version": WORK_FONT_INTENT_SCHEMA_VERSION,
            "policy_version": WORK_INTENT_POLICY_VERSION,
            "work_key": self.work_key,
            "group_kind": self.group_kind,
            "content_revision": self.content_revision,
            "content_keys": self.content_keys,
            "primary_content_key": self.primary_content_key,
            "primary_decision_source": self.primary_decision_source,
            "snapshot_fingerprint": self.snapshot_fingerprint,
            "store_root": str(Path(store_root)),
            "asset_action": WORK_FONT_CONDITIONAL_ACTION,
            "append_policy": WORK_FONT_APPEND_POLICY,
            "assets": tuple(dict(item) for item in assets),
        }


class WorkIntentStore:
    """Small durable pending authority below one WorkCatalog root."""

    def __init__(self, catalog_root: Path, *, create: bool = True) -> None:
        self.catalog_root = Path(catalog_root)
        self.root = self.catalog_root / ".work-intents"
        # Planning observations and execution/Catalog updates serialize on the
        # same work-key lock.  The pending JSON stays separately namespaced.
        self.lock_root = self.catalog_root / ".locks"
        if create:
            self.root.mkdir(parents=True, exist_ok=True)
            self.lock_root.mkdir(parents=True, exist_ok=True)

    def observe(
        self,
        *,
        primary_provider: ProviderIdentity,
        group_kind: str,
        content_keys: Iterable[str],
    ) -> WorkIntentSnapshot:
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock_root.mkdir(parents=True, exist_ok=True)
        work_key = derive_work_intent_key(
            primary_provider=primary_provider,
            group_kind=group_kind,
        )
        normalized = tuple(sorted({self._content_key(value) for value in content_keys}))
        if not normalized:
            raise WorkIntentError("work font intent contains no content identity")
        identity = self._identity(primary_provider, group_kind)
        with self._lock(work_key):
            existing = self.load_optional(work_key)
            if existing is None:
                revision = 1
                combined = normalized
                primary = normalized[0]
                primary_source = (
                    "single_content"
                    if len(normalized) == 1 else "same_plan_deterministic"
                )
            else:
                if any(existing.get(key) != value for key, value in identity.items()):
                    raise WorkIntentError("pending work intent identity drifted")
                old_keys = tuple(existing["content_keys"])
                combined = tuple(sorted(set(old_keys).union(normalized)))
                changed = combined != old_keys
                revision = int(existing["content_revision"]) + int(changed)
                old_primary = existing.get("primary_content_key")
                old_source = existing.get("primary_decision_source")
                if len(combined) == 1:
                    primary = combined[0]
                    primary_source = "single_content"
                elif old_source == "explicit_user" and old_primary in combined:
                    primary = str(old_primary)
                    primary_source = "explicit_user"
                elif (
                    not changed
                    and old_source == "same_plan_deterministic"
                    and old_primary in combined
                ):
                    primary = str(old_primary)
                    primary_source = "same_plan_deterministic"
                else:
                    primary = None
                    primary_source = None
            core = {
                "schema_version": WORK_FONT_INTENT_SCHEMA_VERSION,
                "policy_version": WORK_INTENT_POLICY_VERSION,
                "work_key": work_key,
                **identity,
                "content_revision": revision,
                "content_keys": list(combined),
                "primary_content_key": primary,
                "primary_decision_source": primary_source,
            }
            payload = {
                **core,
                "snapshot_fingerprint": _fingerprint(core),
            }
            if existing != payload:
                self._write(work_key, payload)
            return self._snapshot(payload)

    def load_optional(self, work_key: str) -> Optional[dict[str, object]]:
        path = self._path(work_key)
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkIntentError(f"cannot read pending work intent: {exc}") from exc
        self._validate(payload, work_key)
        return payload

    def load(self, work_key: str) -> WorkIntentSnapshot:
        payload = self.load_optional(work_key)
        if payload is None:
            raise WorkIntentError("pending work intent is missing")
        return self._snapshot(payload)

    @staticmethod
    def _identity(
        primary_provider: ProviderIdentity,
        group_kind: str,
    ) -> dict[str, str]:
        derive_work_intent_key(
            primary_provider=primary_provider,
            group_kind=group_kind,
        )
        return {
            "namespace": str(primary_provider.namespace).casefold(),
            "entity_type": str(primary_provider.entity_type).casefold(),
            "provider_id": str(primary_provider.provider_id),
            "group_kind": str(group_kind),
        }

    @staticmethod
    def _content_key(value: object) -> str:
        size, digest = parse_content_key(value)
        return make_content_key(size=size, digest=digest)

    @staticmethod
    def _snapshot(payload: Mapping[str, object]) -> WorkIntentSnapshot:
        return WorkIntentSnapshot(
            work_key=str(payload["work_key"]),
            content_revision=int(payload["content_revision"]),
            namespace=str(payload["namespace"]),
            entity_type=str(payload["entity_type"]),
            provider_id=str(payload["provider_id"]),
            group_kind=str(payload["group_kind"]),
            content_keys=tuple(str(value) for value in payload["content_keys"]),
            primary_content_key=(
                str(payload["primary_content_key"])
                if payload.get("primary_content_key") is not None else None
            ),
            primary_decision_source=(
                str(payload["primary_decision_source"])
                if payload.get("primary_decision_source") is not None else None
            ),
            snapshot_fingerprint=str(payload["snapshot_fingerprint"]),
        )

    @staticmethod
    def _validate(payload: object, work_key: str) -> None:
        if not isinstance(payload, dict):
            raise WorkIntentError("pending work intent root is invalid")
        keys = payload.get("content_keys")
        if (
            payload.get("schema_version") != WORK_FONT_INTENT_SCHEMA_VERSION
            or payload.get("policy_version") != WORK_INTENT_POLICY_VERSION
            or payload.get("work_key") != work_key
            or not isinstance(keys, list)
            or not keys
            or any(not isinstance(value, str) for value in keys)
            or list(keys) != sorted(set(keys))
            or any(_CONTENT_KEY.fullmatch(str(value)) is None for value in keys)
            or not isinstance(payload.get("content_revision"), int)
            or int(payload["content_revision"]) < 1
            or not all(
                isinstance(payload.get(field), str)
                and bool(str(payload.get(field)).strip())
                for field in (
                    "namespace", "entity_type", "provider_id", "group_kind"
                )
            )
            or payload.get("group_kind") not in {"movie_continuous", "tv_episodes"}
        ):
            raise WorkIntentError("pending work intent schema is invalid")
        primary = payload.get("primary_content_key")
        source = payload.get("primary_decision_source")
        if primary is not None and primary not in keys:
            raise WorkIntentError("pending work intent primary is invalid")
        if (primary is None) != (source is None):
            raise WorkIntentError("pending work intent primary authority is incomplete")
        core = {key: value for key, value in payload.items() if key != "snapshot_fingerprint"}
        if payload.get("snapshot_fingerprint") != _fingerprint(core):
            raise WorkIntentError("pending work intent fingerprint is invalid")

    def _path(self, work_key: str) -> Path:
        if _WORK_KEY.fullmatch(str(work_key)) is None:
            raise WorkIntentError("unsafe work intent key")
        return self.root / f"{work_key}.json"

    @contextmanager
    def _lock(self, work_key: str):
        self._path(work_key)
        try:
            with advisory_file_lock(
                self.lock_root / f"{work_key}.lock",
                owner={"kind": "work_intent", "work_key": work_key},
            ):
                yield
        except FileLockBusyError as exc:
            raise WorkIntentError("pending work intent is busy") from exc

    def _write(self, work_key: str, payload: Mapping[str, object]) -> None:
        destination = self._path(work_key)
        temporary = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.tmp"
        try:
            with open(temporary, "x", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)


def _fingerprint(payload: Mapping[str, object]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
