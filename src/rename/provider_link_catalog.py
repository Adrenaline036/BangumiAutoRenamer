"""Versioned local cross-provider identity links.

Provider links are local, auditable facts.  Remote provider identifiers are
evidence edges; they never replace the stable local Series/Work key.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping, Optional

from .file_lock import FileLockBusyError


PROVIDER_LINK_SCHEMA_VERSION = 1
PROVIDER_LINK_STATUSES = frozenset(
    {
        "inferred",
        "user_confirmed",
        "materialized_confirmed",
        "conflicted",
        "superseded",
    }
)
_RESERVING_STATUSES = frozenset(
    {"inferred", "user_confirmed", "materialized_confirmed"}
)
_SAFE_MAPPING_ID = re.compile(r"^provider-link-[0-9a-f-]{36}$")
_SAFE_SERIES_KEY = re.compile(r"^series-[0-9a-f-]{36}$")
_SAFE_WORK_KEY = re.compile(r"^work-[0-9a-f-]{36}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ProviderLinkCatalogError(RuntimeError):
    pass


def fingerprint_evidence(value: Mapping[str, object]) -> str:
    """Return the canonical SHA-256 used by ProviderLink evidence records."""

    return _fingerprint(value)


@dataclass(frozen=True)
class ProviderLinkMapping:
    schema_version: int
    mapping_id: str
    revision: int
    local_identity_kind: str
    local_identity_key: str
    status: str
    provider_edges: tuple[Mapping[str, object], ...]
    season_edges: tuple[Mapping[str, object], ...]
    evidence_snapshot: Mapping[str, object]
    evidence_fingerprint: str
    confirmed_by: Optional[str] = None
    confirmed_at: Optional[str] = None
    materialized_at: Optional[str] = None

    @property
    def local_series_key(self) -> Optional[str]:
        return (
            self.local_identity_key
            if self.local_identity_kind == "series"
            else None
        )

    @property
    def local_work_key(self) -> Optional[str]:
        return (
            self.local_identity_key
            if self.local_identity_kind == "work"
            else None
        )

    @property
    def reusable_offline(self) -> bool:
        return self.status == "materialized_confirmed"

    @classmethod
    def from_dict(cls, value: object) -> "ProviderLinkMapping":
        if not isinstance(value, Mapping):
            raise ProviderLinkCatalogError("ProviderLinkMapping 根节点不是对象")
        if value.get("schema_version") != PROVIDER_LINK_SCHEMA_VERSION:
            raise ProviderLinkCatalogError(
                "ProviderLinkMapping schema_version 不受支持"
            )

        mapping_id = str(value.get("mapping_id") or "")
        if _SAFE_MAPPING_ID.fullmatch(mapping_id) is None:
            raise ProviderLinkCatalogError("ProviderLinkMapping mapping_id 无效")
        revision = value.get("revision")
        if not isinstance(revision, int) or revision < 0:
            raise ProviderLinkCatalogError("ProviderLinkMapping revision 无效")

        identity = value.get("local_identity")
        identity_kind = ""
        identity_key = ""
        if isinstance(identity, Mapping):
            identity_kind = str(identity.get("kind") or "")
            identity_key = str(identity.get("key") or "")
        series_key = str(value.get("local_series_key") or "")
        work_key = str(value.get("local_work_key") or "")
        if series_key and work_key:
            raise ProviderLinkCatalogError(
                "ProviderLinkMapping 不能同时绑定 Series 与 Work"
            )
        legacy_kind = "series" if series_key else "work" if work_key else ""
        legacy_key = series_key or work_key
        if identity_kind and legacy_kind and (
            identity_kind != legacy_kind or identity_key != legacy_key
        ):
            raise ProviderLinkCatalogError(
                "ProviderLinkMapping 本地身份字段互相冲突"
            )
        identity_kind = identity_kind or legacy_kind
        identity_key = identity_key or legacy_key
        _validate_local_identity(identity_kind, identity_key)

        status = str(value.get("status") or "")
        if status not in PROVIDER_LINK_STATUSES:
            raise ProviderLinkCatalogError("ProviderLinkMapping status 无效")

        provider_edges = _validated_edges(
            value.get("provider_edges"), season=False
        )
        season_edges = _validated_edges(value.get("season_edges"), season=True)
        if identity_kind == "work" and season_edges:
            raise ProviderLinkCatalogError(
                "Work ProviderLinkMapping 不能包含 season_edges"
            )
        if not provider_edges and not season_edges:
            raise ProviderLinkCatalogError(
                "ProviderLinkMapping 至少需要一个 Provider edge"
            )

        snapshot = value.get("evidence_snapshot")
        if not isinstance(snapshot, Mapping):
            raise ProviderLinkCatalogError(
                "ProviderLinkMapping evidence_snapshot 无效"
            )
        fingerprint = str(value.get("evidence_fingerprint") or "")
        if _SHA256.fullmatch(fingerprint) is None:
            raise ProviderLinkCatalogError(
                "ProviderLinkMapping evidence_fingerprint 无效"
            )
        if _fingerprint(snapshot) != fingerprint:
            raise ProviderLinkCatalogError(
                "ProviderLinkMapping evidence fingerprint 不匹配"
            )

        return cls(
            schema_version=PROVIDER_LINK_SCHEMA_VERSION,
            mapping_id=mapping_id,
            revision=revision,
            local_identity_kind=identity_kind,
            local_identity_key=identity_key,
            status=status,
            provider_edges=provider_edges,
            season_edges=season_edges,
            evidence_snapshot=_freeze(snapshot),
            evidence_fingerprint=fingerprint,
            confirmed_by=_optional_text(value.get("confirmed_by")),
            confirmed_at=_optional_text(value.get("confirmed_at")),
            materialized_at=_optional_text(value.get("materialized_at")),
        )

    def to_dict(self) -> dict:
        payload = {
            "schema_version": self.schema_version,
            "mapping_id": self.mapping_id,
            "revision": self.revision,
            "local_identity": {
                "kind": self.local_identity_kind,
                "key": self.local_identity_key,
            },
            "status": self.status,
            "provider_edges": [_thaw(item) for item in self.provider_edges],
            "season_edges": [_thaw(item) for item in self.season_edges],
            "evidence_snapshot": _thaw(self.evidence_snapshot),
            "evidence_fingerprint": self.evidence_fingerprint,
            "confirmed_by": self.confirmed_by,
            "confirmed_at": self.confirmed_at,
            "materialized_at": self.materialized_at,
        }
        payload[
            "local_series_key"
            if self.local_identity_kind == "series"
            else "local_work_key"
        ] = self.local_identity_key
        return payload


@dataclass(frozen=True)
class ProviderLinkMatch:
    mapping: ProviderLinkMapping
    edge_kind: str
    season_number: Optional[int]
    episode_start: Optional[int] = None
    episode_end: Optional[int] = None


class ProviderLinkCatalog:
    def __init__(self, root: Path):
        self.root = Path(root)

    def _lock_resource(self):
        from .file_lock import LockResource
        return LockResource(self, self.root / '.provider-link-catalog.lock', 'provider_link_catalog')

    @contextmanager
    def lock(self):
        from .file_lock import serialized_resource_guard
        try:
            with serialized_resource_guard(
                self._lock_resource(),
                owner={"kind": "provider_link_catalog"},
            ):
                yield
        except FileLockBusyError as exc:
            raise ProviderLinkCatalogError(
                "作品关联登记暂时忙，本次排队未能完成，请稍后重试"
            ) from exc

    def load(self, mapping_id: str) -> ProviderLinkMapping:
        path = self._path(mapping_id)
        try:
            payload = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            raise ProviderLinkCatalogError(
                f"无法读取 ProviderLinkCatalog：{exc}"
            ) from exc
        return ProviderLinkMapping.from_dict(payload)

    def save(
        self,
        mapping: ProviderLinkMapping,
        *,
        expected_revision: int,
    ) -> ProviderLinkMapping:
        if not isinstance(mapping, ProviderLinkMapping):
            raise ProviderLinkCatalogError("save 需要 ProviderLinkMapping")
        with self.lock():
            path = self._path(mapping.mapping_id)
            if expected_revision == -1:
                if path.exists():
                    raise ProviderLinkCatalogError(
                        "ProviderLinkMapping 已存在，不能重复创建"
                    )
                if mapping.revision != 0:
                    raise ProviderLinkCatalogError("新 mapping revision 必须为 0")
                persisted = mapping
            else:
                if expected_revision < 0 or not path.exists():
                    raise ProviderLinkCatalogError(
                        "ProviderLinkMapping 不存在或 expected revision 无效"
                    )
                current = self.load(mapping.mapping_id)
                if current.revision != expected_revision:
                    raise ProviderLinkCatalogError(
                        "ProviderLinkCatalog 版本已变化"
                    )
                if mapping.revision != expected_revision:
                    raise ProviderLinkCatalogError(
                        "待保存 mapping revision 与 expected revision 不一致"
                    )
                persisted = replace(mapping, revision=expected_revision + 1)
            self._validate_unique(persisted)
            self._atomic_write(persisted)
            return persisted

    def find_exact(
        self,
        provider: Mapping[str, object],
        *,
        reusable_only: bool = True,
    ) -> Optional[ProviderLinkMatch]:
        needle = _provider_key(provider)
        if needle is None or not self.root.exists():
            return None
        matches = []
        for mapping in self._all_mappings():
            if reusable_only and not mapping.reusable_offline:
                continue
            match = match_mapping_provider(mapping, provider)
            if match is not None:
                matches.append(match)
        if len(matches) > 1:
            raise ProviderLinkCatalogError(
                "同一 Provider 身份指向多个 ProviderLinkMapping"
            )
        return matches[0] if matches else None

    def list_mappings(self) -> tuple[ProviderLinkMapping, ...]:
        """Return a validated immutable snapshot for resolver anchoring."""

        return self._all_mappings()

    def _validate_unique(self, candidate: ProviderLinkMapping) -> None:
        if candidate.status not in _RESERVING_STATUSES:
            return
        candidate_tokens = _reservation_tokens(candidate)
        for existing in self._all_mappings(exclude=candidate.mapping_id):
            if existing.status not in _RESERVING_STATUSES:
                continue
            collision = candidate_tokens.intersection(
                _reservation_tokens(existing)
            )
            if collision:
                raise ProviderLinkCatalogError(
                    "ProviderLinkCatalog 身份冲突：" + sorted(collision)[0]
                )

    def _all_mappings(
        self, *, exclude: Optional[str] = None
    ) -> tuple[ProviderLinkMapping, ...]:
        if not self.root.exists():
            return ()
        mappings = []
        for path in sorted(self.root.glob("provider-link-*.json")):
            if exclude and path.stem == exclude:
                continue
            try:
                payload = json.loads(path.read_text("utf-8"))
                mappings.append(ProviderLinkMapping.from_dict(payload))
            except (OSError, json.JSONDecodeError, TypeError) as exc:
                raise ProviderLinkCatalogError(
                    f"ProviderLinkCatalog 含损坏记录：{path.name}"
                ) from exc
        return tuple(mappings)

    def _path(self, mapping_id: str) -> Path:
        if _SAFE_MAPPING_ID.fullmatch(str(mapping_id)) is None:
            raise ProviderLinkCatalogError("mapping_id 无效")
        return self.root / f"{mapping_id}.json"

    def _atomic_write(self, mapping: ProviderLinkMapping) -> None:
        destination = self._path(mapping.mapping_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = (
            destination.parent
            / f".{destination.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            with open(temporary, "x", encoding="utf-8") as handle:
                json.dump(
                    mapping.to_dict(),
                    handle,
                    ensure_ascii=False,
                    indent=2,
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)


def match_mapping_provider(
    mapping: ProviderLinkMapping, provider: Mapping[str, object]
) -> Optional[ProviderLinkMatch]:
    needle = _provider_key(provider)
    if needle is None:
        return None
    for edge in mapping.provider_edges:
        if _provider_key(edge) == needle:
            return ProviderLinkMatch(mapping, "provider", None)
    for edge in mapping.season_edges:
        if _provider_key(edge) == needle:
            return ProviderLinkMatch(
                mapping,
                "season",
                int(edge["season_number"]),
                (
                    int(edge["episode_start"])
                    if edge.get("episode_start") is not None else None
                ),
                (
                    int(edge["episode_end"])
                    if edge.get("episode_end") is not None else None
                ),
            )
    return None


def season_edges_from_snapshot(
    snapshot: Mapping[str, object],
) -> tuple[Mapping[str, object], ...]:
    """Project one frozen Plan snapshot into exact Catalog season facts."""

    if not isinstance(snapshot, Mapping):
        raise ProviderLinkCatalogError("跨站系列快照无效")
    tmdb_tv_id = str(snapshot.get("tmdb_tv_id") or "")
    assignments = snapshot.get("season_assignments")
    if not tmdb_tv_id or not isinstance(assignments, (list, tuple)):
        raise ProviderLinkCatalogError("跨站系列季度映射不完整")
    assignment_pairs = set()
    for item in assignments:
        if not isinstance(item, Mapping):
            raise ProviderLinkCatalogError("跨站系列季度映射损坏")
        subject_id = str(item.get("bangumi_subject_id") or "")
        season_number = item.get("season_number")
        if not subject_id or not isinstance(season_number, int):
            raise ProviderLinkCatalogError("跨站系列季度映射损坏")
        pair = (subject_id, season_number)
        if pair in assignment_pairs:
            raise ProviderLinkCatalogError("跨站系列季度映射重复")
        assignment_pairs.add(pair)

    schema_version = snapshot.get("schema_version", 1)
    if not isinstance(schema_version, int) or schema_version < 1:
        raise ProviderLinkCatalogError("跨站系列快照版本无效")
    ranges = snapshot.get("derived_episode_ranges")
    if schema_version >= 2:
        if not isinstance(ranges, (list, tuple)) or not ranges:
            raise ProviderLinkCatalogError("跨站系列分集范围缺失")
        raw_edges = []
        for item in ranges:
            if not isinstance(item, Mapping):
                raise ProviderLinkCatalogError("跨站系列分集范围损坏")
            edge = {
                "namespace": "bangumi",
                "entity_type": "subject",
                "provider_id": str(item.get("bangumi_subject_id") or ""),
                "tmdb_tv_id": str(item.get("tmdb_tv_id") or ""),
                "season_number": item.get("season_number"),
                "episode_start": item.get("episode_start"),
                "episode_end": item.get("episode_end"),
            }
            if edge["tmdb_tv_id"] != tmdb_tv_id:
                raise ProviderLinkCatalogError("跨站系列分集范围作品身份不一致")
            raw_edges.append(edge)
        edges = _validated_edges(tuple(raw_edges), season=True)
        edge_pairs = {
            (str(edge["provider_id"]), int(edge["season_number"]))
            for edge in edges
        }
        if edge_pairs != assignment_pairs:
            raise ProviderLinkCatalogError("季度映射与分集范围不一致")
        by_season: dict[tuple[str, int], list[Mapping[str, object]]] = {}
        for edge in edges:
            by_season.setdefault(
                (str(edge["tmdb_tv_id"]), int(edge["season_number"])), []
            ).append(edge)
        for rows in by_season.values():
            ordered = sorted(rows, key=lambda item: int(item["episode_start"]))
            for previous, current in zip(ordered, ordered[1:]):
                if int(current["episode_start"]) != int(previous["episode_end"]) + 1:
                    raise ProviderLinkCatalogError("同季度分集范围存在重叠或空档")
        return edges

    return _validated_edges(
        tuple(
            {
                "namespace": "bangumi",
                "entity_type": "subject",
                "provider_id": subject_id,
                "tmdb_tv_id": tmdb_tv_id,
                "season_number": season_number,
            }
            for subject_id, season_number in sorted(assignment_pairs)
        ),
        season=True,
    )


def mapping_materializes_snapshot(
    mapping: ProviderLinkMapping,
    snapshot: Mapping[str, object],
) -> bool:
    """Return whether a durable mapping contains every exact frozen fact."""

    try:
        expected = season_edges_from_snapshot(snapshot)
    except ProviderLinkCatalogError:
        return False
    if (
        mapping.status != "materialized_confirmed"
        or mapping.mapping_id != str(snapshot.get("mapping_id") or "")
        or mapping.local_series_key != str(snapshot.get("series_key") or "")
    ):
        return False
    expected_tmdb = str(snapshot.get("tmdb_tv_id") or "")
    if not any(
        edge.get("namespace") == "tmdb"
        and edge.get("entity_type") == "tv"
        and str(edge.get("provider_id") or "") == expected_tmdb
        for edge in mapping.provider_edges
    ):
        return False
    current_by_provider = {
        _provider_key(edge): _season_fact(edge) for edge in mapping.season_edges
    }
    return all(
        current_by_provider.get(_provider_key(edge)) == _season_fact(edge)
        for edge in expected
    )


def materialize_provider_link_snapshot(
    catalog: ProviderLinkCatalog,
    snapshot: Mapping[str, object],
) -> ProviderLinkMapping:
    """Persist exact ranges once; replay is a byte-stable semantic no-op."""

    mapping_id = str(snapshot.get("mapping_id") or "")
    series_key = str(snapshot.get("series_key") or "")
    tmdb_tv_id = str(snapshot.get("tmdb_tv_id") or "")
    level = str(snapshot.get("automation_level") or "")
    if level not in {"L1", "L2", "L3"}:
        raise ProviderLinkCatalogError("跨站系列物化级别无效")
    if not mapping_id or not series_key or not tmdb_tv_id:
        raise ProviderLinkCatalogError("跨站系列物化身份不完整")
    expected_edges = season_edges_from_snapshot(snapshot)
    evidence_snapshot = {
        key: value for key, value in snapshot.items()
        if key not in {"mapping_id", "revision", "evidence_fingerprint", "status"}
    }
    anchor = snapshot.get("anchor")
    if isinstance(anchor, Mapping) and anchor.get("mapping_id"):
        if str(anchor.get("mapping_id")) != mapping_id:
            raise ProviderLinkCatalogError("跨站系列 anchor 身份不一致")
        current = catalog.load(mapping_id)
        anchor_revision = anchor.get("revision")
        anchor_fingerprint = str(anchor.get("evidence_fingerprint") or "")
        if (
            isinstance(anchor_revision, int)
            and current.revision == anchor_revision + 1
            and mapping_materializes_snapshot(current, snapshot)
        ):
            return current
        if (
            not isinstance(anchor_revision, int)
            or current.revision != anchor_revision
            or current.evidence_fingerprint != anchor_fingerprint
        ):
            raise ProviderLinkCatalogError("跨站系列 anchor 已变化")
        provider_edges = list(current.provider_edges)
        provider_keys = {_provider_key(edge) for edge in provider_edges}
        current_seasons = {
            _provider_key(edge): _season_fact(edge)
            for edge in current.season_edges
        }
        merged_seasons = list(current.season_edges)
        for edge in expected_edges:
            key = _provider_key(edge)
            existing = current_seasons.get(key)
            if existing is not None and existing != _season_fact(edge):
                raise ProviderLinkCatalogError("同一季度登记的分集范围不一致")
            if existing is None:
                merged_seasons.append(edge)
            if key not in provider_keys:
                provider_edges.append({
                    "namespace": "bangumi",
                    "entity_type": "subject",
                    "provider_id": str(edge["provider_id"]),
                    "role": "season",
                })
                provider_keys.add(key)
        previous_evidence = current.to_dict()["evidence_snapshot"]
        if (
            isinstance(previous_evidence, dict)
            and isinstance(previous_evidence.get("evidence_history"), list)
            and isinstance(previous_evidence.get("latest_evidence"), dict)
        ):
            evidence_history = list(previous_evidence["evidence_history"])
            evidence_history.append(previous_evidence["latest_evidence"])
        else:
            evidence_history = [previous_evidence]
        combined_evidence = {
            "evidence_history": evidence_history,
            "latest_evidence": evidence_snapshot,
        }
        payload = current.to_dict()
        payload.update({
            "status": "materialized_confirmed",
            "provider_edges": provider_edges,
            "season_edges": merged_seasons,
            "evidence_snapshot": combined_evidence,
            "evidence_fingerprint": fingerprint_evidence(combined_evidence),
        })
        return catalog.save(
            ProviderLinkMapping.from_dict(payload),
            expected_revision=current.revision,
        )

    path = catalog._path(mapping_id)
    if path.exists():
        current = catalog.load(mapping_id)
        if mapping_materializes_snapshot(current, snapshot):
            return current
        raise ProviderLinkCatalogError("既有跨站系列登记与批准计划不一致")
    payload = {
        "schema_version": 1,
        "mapping_id": mapping_id,
        "revision": 0,
        "local_series_key": series_key,
        "status": "materialized_confirmed",
        "provider_edges": ({
            "namespace": "tmdb",
            "entity_type": "tv",
            "provider_id": tmdb_tv_id,
            "role": "series",
        },),
        "season_edges": expected_edges,
        "evidence_snapshot": evidence_snapshot,
        "evidence_fingerprint": str(snapshot.get("evidence_fingerprint") or ""),
    }
    return catalog.save(
        ProviderLinkMapping.from_dict(payload), expected_revision=-1
    )


def _season_fact(edge: Mapping[str, object]) -> tuple[object, ...]:
    return (
        str(edge.get("tmdb_tv_id") or ""),
        edge.get("season_number"),
        edge.get("episode_start"),
        edge.get("episode_end"),
    )


def _validate_local_identity(kind: str, key: str) -> None:
    matcher = _SAFE_SERIES_KEY if kind == "series" else _SAFE_WORK_KEY
    if kind not in {"series", "work"} or matcher.fullmatch(key) is None:
        raise ProviderLinkCatalogError(
            "ProviderLinkMapping 本地 Series/Work 身份无效"
        )


def _validated_edges(value: object, *, season: bool) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, (list, tuple)):
        label = "season_edges" if season else "provider_edges"
        raise ProviderLinkCatalogError(f"ProviderLinkMapping {label} 无效")
    edges = []
    provider_keys = set()
    for raw in value:
        if not isinstance(raw, Mapping) or _provider_key(raw) is None:
            raise ProviderLinkCatalogError("ProviderLinkMapping edge 身份无效")
        provider_key = _provider_key(raw)
        if provider_key in provider_keys:
            raise ProviderLinkCatalogError(
                "ProviderLinkMapping 内存在重复 Provider edge"
            )
        provider_keys.add(provider_key)
        if season:
            tmdb_tv_id = str(raw.get("tmdb_tv_id") or "")
            season_number = raw.get("season_number")
            if not tmdb_tv_id or not isinstance(season_number, int) or season_number < 0:
                raise ProviderLinkCatalogError(
                    "ProviderLinkMapping season edge 无效"
                )
            episode_start = raw.get("episode_start")
            episode_end = raw.get("episode_end")
            if (episode_start is None) != (episode_end is None):
                raise ProviderLinkCatalogError(
                    "episode range 必须同时提供起止值"
                )
            if episode_start is not None and (
                not isinstance(episode_start, int)
                or not isinstance(episode_end, int)
                or episode_start < 1
                or episode_end < episode_start
            ):
                raise ProviderLinkCatalogError("episode range 无效")
        elif not str(raw.get("role") or ""):
            raise ProviderLinkCatalogError("provider edge 缺少 role")
        edges.append(_freeze(raw))
    return tuple(edges)


def _reservation_tokens(mapping: ProviderLinkMapping) -> set[str]:
    tokens = {
        f"local:{mapping.local_identity_kind}:{mapping.local_identity_key}"
    }
    for edge in mapping.provider_edges:
        key = _provider_key(edge)
        if key is not None:
            tokens.add("provider:" + ":".join(key))
    for edge in mapping.season_edges:
        key = _provider_key(edge)
        if key is not None:
            tokens.add("provider:" + ":".join(key))
        tokens.add(
            "tmdb-season:"
            + str(edge["tmdb_tv_id"])
            + ":"
            + str(edge["season_number"])
        )
    return tokens


def _provider_key(
    value: Mapping[str, object],
) -> Optional[tuple[str, str, str]]:
    fields = tuple(
        str(value.get(key) or "")
        for key in ("namespace", "entity_type", "provider_id")
    )
    return fields if all(fields) else None


def _fingerprint(value: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(
            _thaw(_freeze(value)),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value):
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _optional_text(value: object) -> Optional[str]:
    text = str(value or "").strip()
    return text or None
