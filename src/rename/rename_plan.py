"""Pure data contracts for rename planning.

This module must stay free of filesystem writes.  It converts an already
computed source-to-target mapping into an immutable plan that can be reviewed
and verified before :mod:`src.rename.trans` performs any transfer.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field, replace
from enum import Enum
from functools import cached_property
from pathlib import Path
from types import MappingProxyType
from typing import Dict, Iterable, Optional, Tuple
from collections.abc import Mapping

from .subtitle_labels import DimensionResolution, SUBTITLE_SUFFIXES
from .utils import VIDEO_SUFFIX


_EPISODE_TOKEN = re.compile(r"(?i)(?<![A-Z0-9])S(?P<season>\d+)E(?P<episode>\d+)(?!\d)")
_SEASON_DIRECTORY = re.compile(r"(?i)^Season(?P<season>-?\d+)$")
PLAN_SCHEMA_VERSION = 7
PROJECTION_RECEIPT_SCHEMA_VERSION = 1
SUBTITLE_OVERRIDE_RULE_SCHEMA_VERSION = 1
SUBTITLE_OVERRIDE_ENGINE_VERSION = "0.3.0-dev.s4.1"


class PlanStatus(str, Enum):
    """Shared result vocabulary for planning and verification."""

    ACCEPTED = "accepted"
    FAIL_CLOSED = "fail_closed"
    NEED_CONFIRM = "need_confirm"
    INVALID = "invalid"


@dataclass(frozen=True)
class ProjectionReceipt:
    """Typed, immutable proof that one Plan came from frozen authority."""

    authority: Mapping[str, object]
    fingerprint: str
    schema_version: int = PROJECTION_RECEIPT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != PROJECTION_RECEIPT_SCHEMA_VERSION:
            raise ValueError("ProjectionReceipt schema_version is unsupported")
        if re.fullmatch(r"[0-9a-f]{64}", self.fingerprint) is None:
            raise ValueError("ProjectionReceipt fingerprint is invalid")
        object.__setattr__(self, "authority", _deep_freeze_mapping(self.authority))

    def to_dict(self) -> Dict[str, object]:
        authority = _deep_thaw(self.authority)
        assert isinstance(authority, dict)
        return {
            "schema_version": self.schema_version,
            **authority,
            "authority": authority,
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "ProjectionReceipt":
        authority = value.get("authority")
        if not isinstance(authority, Mapping):
            raise ValueError("ProjectionReceipt authority must be an object")
        return cls(
            authority=dict(authority),
            fingerprint=str(value.get("fingerprint") or ""),
            schema_version=int(value.get("schema_version", 0)),
        )


class UnknownTokenAction(str, Enum):
    PRESERVE_AS_TITLE = "preserve_as_title"
    DISCARD_MANUAL = "discard_manual"


class SubtitleOverrideScope(str, Enum):
    FILE = "file"
    SERIES = "series"


@dataclass(frozen=True)
class ProviderIdentity:
    status: str
    namespace: Optional[str]
    entity_type: Optional[str]
    provider_id: Optional[str]
    title: Optional[str]
    year: Optional[int]
    decision_source: str
    evidence: Tuple[str, ...] = ()
    original_title: Optional[str] = None
    aliases: Tuple[str, ...] = ()
    poster_url: Optional[str] = None
    source_url: Optional[str] = None
    snapshot_at: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", tuple(self.evidence))
        object.__setattr__(self, "aliases", tuple(self.aliases))

    def to_dict(self) -> Dict[str, object]:
        return {
            "status": self.status,
            "namespace": self.namespace,
            "entity_type": self.entity_type,
            "provider_id": self.provider_id,
            "title": self.title,
            "year": self.year,
            "decision_source": self.decision_source,
            "evidence": list(self.evidence),
            "original_title": self.original_title,
            "aliases": list(self.aliases),
            "poster_url": self.poster_url,
            "source_url": self.source_url,
            "snapshot_at": self.snapshot_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "ProviderIdentity":
        evidence = _string_tuple(data.get("evidence", ()), "provider.evidence")
        year = _optional_int(data.get("year"), "provider.year")
        aliases = _string_tuple(data.get("aliases", ()), "provider.aliases")
        fields = {}
        for name in (
            "namespace", "entity_type", "provider_id", "title",
            "original_title", "poster_url", "source_url", "snapshot_at",
        ):
            value = data.get(name)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"Provider {name} must be a string or null")
            fields[name] = value
        return cls(
            status=str(data.get("status", "")),
            year=year,
            decision_source=str(data.get("decision_source", "")),
            evidence=evidence,
            aliases=aliases,
            **fields,
        )


@dataclass(frozen=True)
class WorkIdentity:
    """Stable local work identity, independent from playback layout."""

    status: str
    domain: str
    title: str
    year: Optional[int]
    primary_provider: ProviderIdentity
    domain_source: str = "unknown"

    def to_dict(self) -> Dict[str, object]:
        return {
            "status": self.status,
            "domain": self.domain,
            "title": self.title,
            "year": self.year,
            "domain_source": self.domain_source,
            "primary_provider": self.primary_provider.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "WorkIdentity":
        provider = data.get("primary_provider")
        if not isinstance(provider, Mapping):
            raise ValueError("work_identity.primary_provider must be an object")
        return cls(
            status=str(data.get("status", "")),
            domain=str(data.get("domain", "")),
            title=str(data.get("title", "")),
            year=_optional_int(data.get("year"), "work_identity.year"),
            primary_provider=ProviderIdentity.from_dict(provider),
            domain_source=str(data.get("domain_source", "unknown")),
        )


def derive_work_domain(
    metadata: Mapping[str, object],
    provider: Optional[ProviderIdentity] = None,
) -> Tuple[str, str]:
    """Return the canonical search domain and its auditable source.

    ``metadata.is_anime`` is legacy/pre-identity input evidence only.  Once a
    WorkIdentity exists, its domain is the single persisted fact and must not
    be used to infer playback layout.
    """

    if (
        provider is not None
        and provider.namespace == "bangumi"
        and provider.entity_type == "subject"
    ):
        return "animation", "provider_selection"
    if metadata.get("is_anime") is True:
        return "animation", "task_metadata"
    if metadata.get("is_anime") is False and "is_anime" in metadata:
        return "general", "task_metadata"
    return "unknown", "unknown"


@dataclass(frozen=True)
class PresentationGroup:
    group_id: str
    kind: str
    display_name: str
    preferred_version_ref: Optional[str]
    provider_identity: Optional[ProviderIdentity] = None

    def to_dict(self) -> Dict[str, object]:
        return {
            "group_id": self.group_id,
            "kind": self.kind,
            "display_name": self.display_name,
            "preferred_version_ref": self.preferred_version_ref,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "PresentationGroup":
        provider = data.get("provider_identity")
        preferred = data.get("preferred_version_ref")
        if provider is not None and not isinstance(provider, Mapping):
            raise ValueError("Presentation group provider_identity must be an object")
        if preferred is not None and not isinstance(preferred, str):
            raise ValueError("preferred_version_ref must be a string or null")
        return cls(
            group_id=str(data.get("group_id", "")),
            kind=str(data.get("kind", "")),
            display_name=str(data.get("display_name", "")),
            preferred_version_ref=preferred,
            provider_identity=(
                ProviderIdentity.from_dict(provider)
                if isinstance(provider, Mapping) else None
            ),
        )


@dataclass(frozen=True)
class EditionValue:
    key: str
    label: Optional[str] = None

    def to_dict(self) -> Dict[str, object]:
        return {"key": self.key, "label": self.label}

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "EditionValue":
        label = data.get("label")
        if label is not None and not isinstance(label, str):
            raise ValueError("Edition label must be a string or null")
        return cls(str(data.get("key", "")), label)


@dataclass(frozen=True)
class EvidenceCandidate:
    value: object
    source: str
    evidence: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", tuple(self.evidence))

    def to_dict(self) -> Dict[str, object]:
        value = self.value.to_dict() if isinstance(self.value, EditionValue) else self.value
        return {"value": value, "source": self.source, "evidence": list(self.evidence)}


@dataclass(frozen=True)
class EvidenceValue:
    status: str
    value: object = None
    selected_source: Optional[str] = None
    candidates: Tuple[EvidenceCandidate, ...] = ()
    manual_reason: Optional[str] = None
    kind: str = "evidence"

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidates", tuple(self.candidates))

    @classmethod
    def unknown(cls) -> "EvidenceValue":
        return cls(status="unknown")

    def to_dict(self) -> Dict[str, object]:
        value = self.value.to_dict() if isinstance(self.value, EditionValue) else self.value
        return {
            "status": self.status,
            "value": value,
            "selected_source": self.selected_source,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "manual_reason": self.manual_reason,
            "kind": self.kind,
        }

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, object],
        *,
        edition: bool = False,
    ) -> "EvidenceValue":
        raw_value = data.get("value")
        if edition and isinstance(raw_value, Mapping):
            value = EditionValue.from_dict(raw_value)
        else:
            value = raw_value
        raw_candidates = data.get("candidates", ())
        if not isinstance(raw_candidates, (list, tuple)) or not all(
            isinstance(item, Mapping) for item in raw_candidates
        ):
            raise ValueError("Evidence candidates must be objects")
        candidates = []
        for item in raw_candidates:
            candidate_value = item.get("value")
            if edition and isinstance(candidate_value, Mapping):
                candidate_value = EditionValue.from_dict(candidate_value)
            candidates.append(EvidenceCandidate(
                value=candidate_value,
                source=str(item.get("source", "")),
                evidence=_string_tuple(
                    item.get("evidence", ()), "evidence.candidate.evidence"
                ),
            ))
        selected = data.get("selected_source")
        reason = data.get("manual_reason")
        if selected is not None and not isinstance(selected, str):
            raise ValueError("Evidence selected_source must be a string or null")
        if reason is not None and not isinstance(reason, str):
            raise ValueError("Evidence manual_reason must be a string or null")
        return cls(
            status=str(data.get("status", "")),
            value=value,
            selected_source=selected,
            candidates=tuple(candidates),
            manual_reason=reason,
            kind=str(data.get("kind", "evidence")),
        )


@dataclass(frozen=True)
class VersionDescriptor:
    version_ref: Optional[str]
    edition: EvidenceValue
    technical_variant_key: Optional[str]
    resolution: EvidenceValue
    dynamic_range: EvidenceValue
    key_audio: EvidenceValue
    display_label: Optional[str]
    identity_policy: Optional[str] = None

    def to_dict(self) -> Dict[str, object]:
        return {
            "version_ref": self.version_ref,
            **({'identity_policy': self.identity_policy} if self.identity_policy is not None else {}),
            "edition": self.edition.to_dict(),
            "technical_variant_key": self.technical_variant_key,
            "resolution": self.resolution.to_dict(),
            "dynamic_range": self.dynamic_range.to_dict(),
            "key_audio": self.key_audio.to_dict(),
            "display_label": self.display_label,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "VersionDescriptor":
        fields = {}
        for name in ("edition", "resolution", "dynamic_range", "key_audio"):
            raw = data.get(name)
            if not isinstance(raw, Mapping):
                raise ValueError(f"Version descriptor {name} must be an object")
            fields[name] = EvidenceValue.from_dict(raw, edition=name == "edition")
        version_ref = data.get("version_ref")
        technical_key = data.get("technical_variant_key")
        display_label = data.get("display_label")
        for name, value in (
            ("version_ref", version_ref),
            ("technical_variant_key", technical_key),
            ("display_label", display_label),
        ):
            if value is not None and not isinstance(value, str):
                raise ValueError(f"Version descriptor {name} must be string or null")
        return cls(
            version_ref=version_ref,
            identity_policy=data.get('identity_policy'),
            technical_variant_key=technical_key,
            display_label=display_label,
            **fields,
        )


@dataclass(frozen=True)
class AssetRole:
    kind: str
    subtype: Optional[str] = None

    def to_dict(self) -> Dict[str, object]:
        return {"kind": self.kind, "subtype": self.subtype}

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "AssetRole":
        subtype = data.get("subtype")
        if subtype is not None and not isinstance(subtype, str):
            raise ValueError("Asset role subtype must be a string or null")
        return cls(str(data.get("kind", "")), subtype)


@dataclass(frozen=True)
class SegmentDescriptor:
    scheme: str
    index: int
    total: int

    def to_dict(self) -> Dict[str, object]:
        return {"scheme": self.scheme, "index": self.index, "total": self.total}

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "SegmentDescriptor":
        return cls(
            str(data.get("scheme", "")),
            int(data.get("index", 0)),
            int(data.get("total", 0)),
        )


@dataclass(frozen=True)
class CollisionResolution:
    """Approved per-entry disposition for a target-path collision.

    Normal entries keep this field ``None`` and must create a new target.
    ``reuse_existing`` is the only action which permits an existing target;
    media reuses the same physical hardlink, while small companion files use
    explicit content evidence. Execution revalidates the approved decision.
    """

    action: str
    source_size: int
    target_size: int
    target_device: Optional[int] = None
    target_inode: Optional[int] = None
    evidence_kind: str = "hardlink-identity-v1"
    source_xxh3_128: Optional[str] = None
    target_xxh3_128: Optional[str] = None

    @property
    def content_key(self):
        if self.evidence_kind == 'content-xxh3-128-v1':
            return f'xxh3-128:{self.target_size}:{self.target_xxh3_128}'
        return None

    def to_dict(self) -> Dict[str, object]:
        if self.evidence_kind == 'content-xxh3-128-v1':
            return {'action':self.action,'evidence_kind':self.evidence_kind,
                    'source_size':self.source_size,'source_xxh3_128':self.source_xxh3_128,
                    'target_size':self.target_size,'target_xxh3_128':self.target_xxh3_128,
                    'target_device':self.target_device,'target_inode':self.target_inode}
        if self.evidence_kind == 'hardlink-identity-v1':
            return {"action": self.action, "evidence_kind": self.evidence_kind,
                    "source_size": self.source_size, "target_size": self.target_size,
                    "target_device": self.target_device, "target_inode": self.target_inode}
        raise ValueError('目标复用证据格式不受支持')

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "CollisionResolution":
        action = data.get("action")
        if action != "reuse_existing":
            raise ValueError("collision_resolution.action is not supported")
        if data.get('evidence_kind') == 'content-xxh3-128-v1':
            from .small_file_identity import SmallFileFingerprint
            if set(data) != {'action','evidence_kind','source_size','source_xxh3_128',
                             'target_size','target_xxh3_128','target_device','target_inode'}:
                raise ValueError('小文件复用证据不得混入旧摘要或未知字段')
            target=SmallFileFingerprint(data['target_size'],data['target_xxh3_128'],
                                        data['target_device'],data['target_inode'])
            source=SmallFileFingerprint(data['source_size'],data['source_xxh3_128'],target.device,target.inode)
            if (source.size,source.xxh3_128) != (target.size,target.xxh3_128):
                raise ValueError('小文件复用内容不一致')
            return cls(action,source.size,target.size,target.device,target.inode,
                       target.kind,source.xxh3_128,target.xxh3_128)
        if data.get('evidence_kind') == 'hardlink-identity-v1':
            from .media_file_identity import MediaFileIdentity
            if set(data) != {'action', 'evidence_kind', 'source_size', 'target_size', 'target_device', 'target_inode'}:
                raise ValueError('媒体复用证据不得混入内容摘要或未知字段')
            entity = MediaFileIdentity(data['target_size'], data['target_device'], data['target_inode'])
            if type(data['source_size']) is not int or data['source_size'] != entity.size:
                raise ValueError('媒体复用大小不一致')
            return cls(action, entity.size, entity.size, entity.device, entity.inode, entity.kind)
        raise ValueError('目标复用证据格式不受支持')


@dataclass(frozen=True)
class PresentationAssetPlanContract:
    presentation_group: str
    asset_role: AssetRole
    segment: Optional[SegmentDescriptor]
    version_descriptor: Optional[VersionDescriptor]
    owner_version_ref: Optional[str] = None

    def to_dict(self) -> Dict[str, object]:
        return {
            "presentation_group": self.presentation_group,
            "asset_role": self.asset_role.to_dict(),
            "segment": self.segment.to_dict() if self.segment else None,
            "version_descriptor": (
                self.version_descriptor.to_dict() if self.version_descriptor else None
            ),
            "owner_version_ref": self.owner_version_ref,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "PresentationAssetPlanContract":
        role = data.get("asset_role")
        segment = data.get("segment")
        descriptor = data.get("version_descriptor")
        owner_version_ref = data.get("owner_version_ref")
        if not isinstance(role, Mapping):
            raise ValueError("presentation_asset.asset_role must be an object")
        if segment is not None and not isinstance(segment, Mapping):
            raise ValueError("presentation_asset.segment must be object or null")
        if descriptor is not None and not isinstance(descriptor, Mapping):
            raise ValueError("presentation_asset.version_descriptor must be object or null")
        if owner_version_ref is not None and not isinstance(owner_version_ref, str):
            raise ValueError("presentation_asset.owner_version_ref must be string or null")
        return cls(
            presentation_group=str(data.get("presentation_group", "")),
            asset_role=AssetRole.from_dict(role),
            segment=(SegmentDescriptor.from_dict(segment) if isinstance(segment, Mapping) else None),
            version_descriptor=(
                VersionDescriptor.from_dict(descriptor)
                if isinstance(descriptor, Mapping) else None
            ),
            owner_version_ref=owner_version_ref,
        )


def build_version_ref(
    presentation_group: str,
    season_id: Optional[int],
    episode_id: Optional[int],
    edition_key: str,
    technical_variant_key: str,
) -> str:
    payload = {
        "presentation_group": presentation_group,
        "season_id": season_id,
        "episode_id": episode_id,
        "edition_key": edition_key,
        "technical_variant_key": technical_variant_key,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "v-" + hashlib.sha256(canonical).hexdigest()[:20]


_PRESENTATION_RESERVED_LABELS = {
    "电影连续版", "tv 分集版", "光盘结构",
    "movie continuous", "tv episodes", "disc structure",
    "movie_continuous", "tv_episodes", "disc_structure",
}


def _normalized_label_part(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    normalized = " ".join(unicodedata.normalize("NFKC", value).strip().split())
    return normalized or None


def format_presentation_version_label(
    edition_label: Optional[str],
    resolution: Optional[str],
    dynamic_range: Optional[str],
    key_audio: Optional[str],
) -> Optional[str]:
    parts = tuple(
        part for part in (
            _normalized_label_part(edition_label),
            _normalized_label_part(resolution),
            _normalized_label_part(dynamic_range),
            _normalized_label_part(key_audio),
        ) if part is not None
    )
    reserved = {
        " ".join(unicodedata.normalize("NFKC", value).casefold().split())
        for value in _PRESENTATION_RESERVED_LABELS
    }
    if any(" ".join(part.casefold().split()) in reserved for part in parts):
        raise ValueError("Presentation group terms cannot be version labels")
    return " ".join(parts) if parts else None


def build_default_version_descriptor(
    presentation_group: str,
    season_id: Optional[int],
    episode_id: Optional[int],
) -> VersionDescriptor:
    edition = EvidenceValue(
        status="resolved",
        value=EditionValue("default", None),
        selected_source=None,
        candidates=(),
        manual_reason=None,
        kind="structural_default",
    )
    return VersionDescriptor(
        version_ref=build_version_ref(
            presentation_group,
            season_id,
            episode_id,
            "default",
            "default",
        ),
        edition=edition,
        technical_variant_key="default",
        resolution=EvidenceValue.unknown(),
        dynamic_range=EvidenceValue.unknown(),
        key_audio=EvidenceValue.unknown(),
        display_label=None,
    )


@dataclass(frozen=True)
class ExecutionPolicy:
    """Immutable execution semantics bound into the approved Plan fingerprint."""

    target_root: Path
    mode: str = "hardlink"
    dry_run: bool = False
    preserve_source: bool = True
    schema_version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "target_root", Path(self.target_root))
        if self.mode != "hardlink":
            raise ValueError("S5 execution policy only supports hardlink mode")
        if self.preserve_source is not True:
            raise ValueError("S5 execution policy must preserve every source")
        if not isinstance(self.dry_run, bool) or self.schema_version != 1:
            raise ValueError("Unsupported execution policy")

    def to_dict(self) -> Dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "target_root": str(self.target_root),
            "mode": self.mode,
            "dry_run": self.dry_run,
            "preserve_source": self.preserve_source,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "ExecutionPolicy":
        target_root = data.get("target_root")
        mode = data.get("mode")
        dry_run = data.get("dry_run")
        preserve_source = data.get("preserve_source")
        schema_version = data.get("schema_version")
        if not isinstance(target_root, str) or not target_root:
            raise ValueError("Execution policy target_root must be a string")
        if not isinstance(mode, str) or not isinstance(dry_run, bool):
            raise ValueError("Execution policy mode/dry_run types are invalid")
        if not isinstance(preserve_source, bool) or isinstance(schema_version, bool):
            raise ValueError("Execution policy preserve_source/schema types are invalid")
        return cls(
            target_root=Path(target_root),
            mode=mode,
            dry_run=dry_run,
            preserve_source=preserve_source,
            schema_version=int(schema_version),
        )


@dataclass(frozen=True)
class UnknownTokenDisposition:
    token: str
    action: UnknownTokenAction
    reason: str = ""

    def to_dict(self) -> Dict[str, object]:
        return {"token": self.token, "action": self.action.value, "reason": self.reason}

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "UnknownTokenDisposition":
        token = data.get("token")
        action = data.get("action")
        reason = data.get("reason", "")
        if not isinstance(token, str) or not isinstance(reason, str):
            raise ValueError("Subtitle unknown-token disposition fields must be strings")
        try:
            parsed_action = UnknownTokenAction(action)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Unsupported unknown-token action: {action!r}") from exc
        return cls(token=token, action=parsed_action, reason=reason)


@dataclass(frozen=True)
class SeriesScopeIdentity:
    provider_namespace: str
    provider_media_type: str
    provider_id: str
    scope_id: str

    @classmethod
    def from_series_key(cls, series_key: object) -> "SeriesScopeIdentity":
        value = str(series_key).strip()
        if not value:
            raise ValueError("Series scope requires a confirmed series key")
        import hashlib
        scope = hashlib.sha256(
            f"bar\0series\0{value}".encode("utf-8")
        ).hexdigest()
        return cls("bar", "series", value, scope)

    @classmethod
    def from_provider(cls, namespace: str, media_type: str, provider_id: object) -> "SeriesScopeIdentity":
        namespace = str(namespace).strip().casefold()
        media_type = str(media_type).strip().casefold()
        value = str(provider_id).strip()
        if (
            (namespace, media_type) not in {
                ("tmdb", "tv"),
                ("bangumi", "subject"),
                ("bar", "series"),
            }
            or not value
        ):
            raise ValueError(
                "Series scope requires a confirmed TV work identity"
            )
        import hashlib
        scope = hashlib.sha256(f"{namespace}\0{media_type}\0{value}".encode("utf-8")).hexdigest()
        return cls(namespace, media_type, value, scope)

    def to_dict(self) -> Dict[str, str]:
        return {
            "provider_namespace": self.provider_namespace,
            "provider_media_type": self.provider_media_type,
            "provider_id": self.provider_id,
            "scope_id": self.scope_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "SeriesScopeIdentity":
        namespace = str(data.get("provider_namespace", ""))
        media_type = str(data.get("provider_media_type", ""))
        provider_id = data.get("provider_id", "")
        expected = (
            cls.from_series_key(provider_id)
            if (namespace.casefold(), media_type.casefold())
            == ("bar", "series")
            else cls.from_provider(namespace, media_type, provider_id)
        )
        if data.get("scope_id") != expected.scope_id:
            raise ValueError("Series scope identity fingerprint mismatch")
        return expected


@dataclass(frozen=True)
class SubtitleRuleMatcher:
    raw_label_canonical: str
    unknown_tokens_canonical: Tuple[str, ...]
    association_rule_version: str
    label_rule_version: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "unknown_tokens_canonical", tuple(self.unknown_tokens_canonical))

    def to_dict(self) -> Dict[str, object]:
        return {
            "raw_label_canonical": self.raw_label_canonical,
            "unknown_tokens_canonical": list(self.unknown_tokens_canonical),
            "association_rule_version": self.association_rule_version,
            "label_rule_version": self.label_rule_version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "SubtitleRuleMatcher":
        return cls(
            str(data.get("raw_label_canonical", "")),
            _string_tuple(data.get("unknown_tokens_canonical", ()), "matcher.unknown_tokens_canonical"),
            str(data.get("association_rule_version", "")),
            str(data.get("label_rule_version", "")),
        )


@dataclass(frozen=True)
class SubtitleRuleOutcome:
    primary_language: str
    script: Optional[str]
    region_hint: Optional[str] = None
    additional_languages: Tuple[str, ...] = ()
    track_title_tokens: Tuple[str, ...] = ()
    flags: Tuple[str, ...] = ()
    unknown_dispositions: Tuple[UnknownTokenDisposition, ...] = ()

    def __post_init__(self) -> None:
        for name in ("additional_languages", "track_title_tokens", "flags", "unknown_dispositions"):
            object.__setattr__(self, name, tuple(getattr(self, name)))

    def to_dict(self) -> Dict[str, object]:
        return {
            "primary_language": self.primary_language,
            "script": self.script,
            "region_hint": self.region_hint,
            "additional_languages": list(self.additional_languages),
            "track_title_tokens": list(self.track_title_tokens),
            "flags": list(self.flags),
            "unknown_dispositions": [item.to_dict() for item in self.unknown_dispositions],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "SubtitleRuleOutcome":
        dispositions = data.get("unknown_dispositions", ())
        if not isinstance(dispositions, (list, tuple)) or not all(isinstance(item, Mapping) for item in dispositions):
            raise ValueError("rule outcome dispositions must be objects")
        script = data.get("script")
        region = data.get("region_hint")
        if (
            (script is not None and not isinstance(script, str))
            or (region is not None and not isinstance(region, str))
        ):
            raise ValueError("rule outcome script/region must be strings or null")
        return cls(
            primary_language=str(data.get("primary_language", "")),
            script=script,
            region_hint=region,
            additional_languages=_string_tuple(data.get("additional_languages", ()), "outcome.additional_languages"),
            track_title_tokens=_string_tuple(data.get("track_title_tokens", ()), "outcome.track_title_tokens"),
            flags=_string_tuple(data.get("flags", ()), "outcome.flags"),
            unknown_dispositions=tuple(UnknownTokenDisposition.from_dict(item) for item in dispositions),
        )


@dataclass(frozen=True)
class SubtitleOverrideRule:
    rule_id: str
    scope: SubtitleOverrideScope
    scope_id: str
    base_plan_fingerprint: str
    selected_entry_id: str
    matcher: SubtitleRuleMatcher
    outcome: SubtitleRuleOutcome
    reason: str
    rule_fingerprint: str
    schema_version: int = SUBTITLE_OVERRIDE_RULE_SCHEMA_VERSION
    engine_version: str = SUBTITLE_OVERRIDE_ENGINE_VERSION

    def unsigned_dict(self) -> Dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "engine_version": self.engine_version,
            "rule_id": self.rule_id,
            "scope": self.scope.value,
            "scope_id": self.scope_id,
            "base_plan_fingerprint": self.base_plan_fingerprint,
            "selected_entry_id": self.selected_entry_id,
            "matcher": self.matcher.to_dict(),
            "outcome": self.outcome.to_dict(),
            "reason": self.reason,
        }

    def to_dict(self) -> Dict[str, object]:
        return {**self.unsigned_dict(), "rule_fingerprint": self.rule_fingerprint}

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "SubtitleOverrideRule":
        matcher = data.get("matcher")
        outcome = data.get("outcome")
        if not isinstance(matcher, Mapping) or not isinstance(outcome, Mapping):
            raise ValueError("Subtitle rule matcher/outcome must be objects")
        return cls(
            rule_id=str(data.get("rule_id", "")),
            scope=SubtitleOverrideScope(data.get("scope")),
            scope_id=str(data.get("scope_id", "")),
            base_plan_fingerprint=str(data.get("base_plan_fingerprint", "")),
            selected_entry_id=str(data.get("selected_entry_id", "")),
            matcher=SubtitleRuleMatcher.from_dict(matcher),
            outcome=SubtitleRuleOutcome.from_dict(outcome),
            reason=str(data.get("reason", "")),
            rule_fingerprint=str(data.get("rule_fingerprint", "")),
            schema_version=int(data.get("schema_version", 0)),
            engine_version=str(data.get("engine_version", "")),
        )


@dataclass(frozen=True)
class SubtitlePlanContract:
    """Strict, immutable subtitle evidence embedded in schema-v5 plans."""

    associated_video_source_path: Optional[Path]
    raw_label: str
    primary_language: Optional[str]
    script: Optional[str]
    association_candidates: Tuple[Path, ...] = ()
    region_hint: Optional[str] = None
    additional_languages: Tuple[str, ...] = ()
    track_title_tokens: Tuple[str, ...] = ()
    flags: Tuple[str, ...] = ()
    unknown_tokens: Tuple[str, ...] = ()
    unknown_dispositions: Tuple[UnknownTokenDisposition, ...] = ()
    association_state: DimensionResolution = DimensionResolution.UNRESOLVED
    parse_state: DimensionResolution = DimensionResolution.UNRESOLVED
    association_rule_version: str = ""
    label_rule_version: str = ""
    reason_codes: Tuple[str, ...] = ()
    decision_source: str = "automatic"
    manual_reason: str = ""
    applied_rule_id: str = ""
    applied_rule_fingerprint: str = ""
    applied_rule_scope: str = ""
    applied_rule_engine_version: str = ""
    inherited_edit_fingerprint: str = ""

    def __post_init__(self) -> None:
        if (not isinstance(self.inherited_edit_fingerprint, str)
                or self.inherited_edit_fingerprint and re.fullmatch(r'[0-9a-f]{64}', self.inherited_edit_fingerprint) is None):
            raise ValueError('Subtitle inherited_edit_fingerprint is invalid')
        object.__setattr__(
            self,
            "associated_video_source_path",
            Path(self.associated_video_source_path)
            if self.associated_video_source_path is not None else None,
        )
        object.__setattr__(
            self,
            "association_candidates",
            tuple(Path(path) for path in self.association_candidates),
        )
        for name in (
            "additional_languages", "track_title_tokens", "flags",
            "unknown_tokens", "unknown_dispositions", "reason_codes",
        ):
            object.__setattr__(self, name, tuple(getattr(self, name)))

    def to_dict(self) -> Dict[str, object]:
        return {
            "associated_video_source_path": (
                str(self.associated_video_source_path)
                if self.associated_video_source_path is not None else None
            ),
            "association_candidates": [
                str(path) for path in self.association_candidates
            ],
            "raw_label": self.raw_label,
            "primary_language": self.primary_language,
            "script": self.script,
            "region_hint": self.region_hint,
            "additional_languages": list(self.additional_languages),
            "track_title_tokens": list(self.track_title_tokens),
            "flags": list(self.flags),
            "unknown_tokens": list(self.unknown_tokens),
            "unknown_dispositions": [item.to_dict() for item in self.unknown_dispositions],
            "association_state": self.association_state.value,
            "parse_state": self.parse_state.value,
            "association_rule_version": self.association_rule_version,
            "label_rule_version": self.label_rule_version,
            "reason_codes": list(self.reason_codes),
            "decision_source": self.decision_source,
            "manual_reason": self.manual_reason,
            "applied_rule_id": self.applied_rule_id,
            "applied_rule_fingerprint": self.applied_rule_fingerprint,
            "applied_rule_scope": self.applied_rule_scope,
            "applied_rule_engine_version": self.applied_rule_engine_version,
            **({'inherited_edit_fingerprint': self.inherited_edit_fingerprint} if self.inherited_edit_fingerprint else {}),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "SubtitlePlanContract":
        path = data.get("associated_video_source_path")
        if path is not None and (not isinstance(path, str) or not path):
            raise ValueError("Subtitle associated_video_source_path must be string or null")
        raw_label = data.get("raw_label")
        if not isinstance(raw_label, str):
            raise ValueError("Subtitle contract raw_label must be a string")
        raw_candidates = data.get("association_candidates", ())
        if not isinstance(raw_candidates, (list, tuple)) or not all(
            isinstance(item, str) and item for item in raw_candidates
        ):
            raise ValueError("Subtitle association_candidates must be paths")
        string_fields = {}
        for name in ("primary_language", "script", "region_hint"):
            value = data.get(name)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"Subtitle contract {name} must be string or null")
            string_fields[name] = value
        tuple_fields = {}
        for name in (
            "additional_languages", "track_title_tokens", "flags",
            "unknown_tokens", "reason_codes",
        ):
            tuple_fields[name] = _string_tuple(data.get(name, ()), f"subtitle.{name}")
        raw_dispositions = data.get("unknown_dispositions", ())
        if not isinstance(raw_dispositions, (list, tuple)) or not all(
            isinstance(item, Mapping) for item in raw_dispositions
        ):
            raise ValueError("subtitle.unknown_dispositions must be a list of objects")
        try:
            association_state = DimensionResolution(data.get("association_state"))
            parse_state = DimensionResolution(data.get("parse_state"))
        except ValueError as exc:
            raise ValueError("Unsupported subtitle resolution state") from exc
        versions = {}
        for name in (
            "association_rule_version", "label_rule_version", "decision_source", "manual_reason",
            "applied_rule_id", "applied_rule_fingerprint", "applied_rule_scope",
            "applied_rule_engine_version",
            "inherited_edit_fingerprint",
        ):
            value = data.get(name, "")
            if not isinstance(value, str):
                raise ValueError(f"Subtitle contract {name} must be a string")
            versions[name] = value
        return cls(
            associated_video_source_path=Path(path) if isinstance(path, str) else None,
            association_candidates=tuple(Path(item) for item in raw_candidates),
            raw_label=raw_label,
            **string_fields,
            **tuple_fields,
            unknown_dispositions=tuple(
                UnknownTokenDisposition.from_dict(item) for item in raw_dispositions
            ),
            association_state=association_state,
            parse_state=parse_state,
            **versions,
        )


@dataclass(frozen=True)
class PlanEntry:
    """One intended source-to-target operation."""

    source_path: Path
    target_path: Path
    season_id: Optional[int] = None
    episode_id: Optional[int] = None
    media_kind: str = "other"
    role: str = ""
    requires_review: bool = False
    evidence: Tuple[str, ...] = ()
    subtitle: Optional[SubtitlePlanContract] = None
    presentation_asset: Optional[PresentationAssetPlanContract] = None
    collision_resolution: Optional[CollisionResolution] = None
    episode_end: Optional[int] = None

    @classmethod
    def from_paths(cls, source_path: Path, target_path: Path) -> "PlanEntry":
        source = Path(source_path)
        target = Path(target_path)
        season_id, episode_id = _target_identity(target)
        suffix = source.suffix.lower()
        media_kind = "video" if suffix in VIDEO_SUFFIX else (
            "subtitle" if suffix in SUBTITLE_SUFFIXES else "associated"
        )
        return cls(
            source_path=source,
            target_path=target,
            season_id=season_id,
            episode_id=episode_id,
            media_kind=media_kind,
        )

    def to_dict(self) -> Dict[str, object]:
        return {
            **({"episode_end": self.episode_end} if self.episode_end is not None else {}),
            "source_path": str(self.source_path),
            "target_path": str(self.target_path),
            "season_id": self.season_id,
            "episode_id": self.episode_id,
            "media_kind": self.media_kind,
            "role": self.role,
            "requires_review": self.requires_review,
            "evidence": list(self.evidence),
            "subtitle": self.subtitle.to_dict() if self.subtitle is not None else None,
            "presentation_asset": (
                self.presentation_asset.to_dict()
                if self.presentation_asset is not None else None
            ),
            "collision_resolution": (
                self.collision_resolution.to_dict()
                if self.collision_resolution is not None else None
            ),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "PlanEntry":
        try:
            source_path = data["source_path"]
            target_path = data["target_path"]
        except KeyError as exc:
            raise ValueError(f"Plan entry missing field: {exc.args[0]}") from exc
        if not isinstance(source_path, str) or not isinstance(target_path, str):
            raise ValueError("Plan entry paths must be strings")
        evidence = data.get("evidence", ())
        if not isinstance(evidence, (list, tuple)) or not all(
            isinstance(item, str) for item in evidence
        ):
            raise ValueError("Plan entry evidence must be a list of strings")
        media_kind = data.get("media_kind", "other")
        role = data.get("role", "")
        requires_review = data.get("requires_review", False)
        subtitle_data = data.get("subtitle")
        presentation_data = data.get("presentation_asset")
        collision_data = data.get("collision_resolution")
        if not isinstance(media_kind, str) or not isinstance(role, str):
            raise ValueError("Plan entry media_kind and role must be strings")
        if not isinstance(requires_review, bool):
            raise ValueError("Plan entry requires_review must be a boolean")
        if subtitle_data is not None and not isinstance(subtitle_data, Mapping):
            raise ValueError("Plan entry subtitle must be an object or null")
        if presentation_data is not None and not isinstance(presentation_data, Mapping):
            raise ValueError("Plan entry presentation_asset must be an object or null")
        if collision_data is not None and not isinstance(collision_data, Mapping):
            raise ValueError("Plan entry collision_resolution must be an object or null")
        return cls(
            source_path=Path(source_path),
            target_path=Path(target_path),
            season_id=_optional_int(data.get("season_id"), "season_id"),
            episode_id=_optional_int(data.get("episode_id"), "episode_id"),
            episode_end=_optional_int(data.get("episode_end"), "episode_end"),
            media_kind=media_kind,
            role=role,
            requires_review=requires_review,
            evidence=tuple(evidence),
            subtitle=(
                SubtitlePlanContract.from_dict(subtitle_data)
                if isinstance(subtitle_data, Mapping) else None
            ),
            presentation_asset=(
                PresentationAssetPlanContract.from_dict(presentation_data)
                if isinstance(presentation_data, Mapping) else None
            ),
            collision_resolution=(
                CollisionResolution.from_dict(collision_data)
                if isinstance(collision_data, Mapping) else None
            ),
        )


@dataclass(frozen=True)
class RenamePlan:
    """Immutable rename plan produced before any media transfer."""

    task_id: str
    source_root: Path
    entries: Tuple[PlanEntry, ...]
    expected_sources: Tuple[Path, ...] = ()
    ignored_sources: Tuple[Path, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)
    execution_policy: Optional[ExecutionPolicy] = None
    series_scope: Optional[SeriesScopeIdentity] = None
    subtitle_override_rules: Tuple[SubtitleOverrideRule, ...] = ()
    revision_parent_fingerprint: str = ""
    revision_action: str = ""
    revision_reason: str = ""
    presentation_groups: Tuple[PresentationGroup, ...] = ()
    work_identity: Optional[WorkIdentity] = None
    linked_provider_identities: Tuple[ProviderIdentity, ...] = ()
    projection_receipt: Optional[ProjectionReceipt] = None

    @cached_property
    def parsed_content_selection(self):
        """Pure choice parsed once for this deeply frozen Plan, not live evidence."""
        from .content_selection import ContentSelection
        raw = self.metadata.get('content_selection')
        return ContentSelection.from_mapping(raw) if raw is not None else None

    @cached_property
    def initial_source_basis(self):
        """Parse this object's frozen metadata once, never cache live file checks.

        Replacement and deserialization create new Plan objects; the property
        is not a dataclass field and never enters persisted data or fingerprints.
        """
        from .source_basis import InitialSourceBasis
        return InitialSourceBasis.from_mapping(self.metadata.get('initial_source_basis'))

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_root", Path(self.source_root))
        object.__setattr__(self, "entries", tuple(self.entries))
        object.__setattr__(
            self,
            "expected_sources",
            tuple(Path(path) for path in self.expected_sources),
        )
        object.__setattr__(
            self,
            "ignored_sources",
            tuple(Path(path) for path in self.ignored_sources),
        )
        object.__setattr__(self, "metadata", _deep_freeze_mapping(self.metadata))
        object.__setattr__(self, "subtitle_override_rules", tuple(self.subtitle_override_rules))
        groups = tuple(self.presentation_groups)
        links = tuple(self.linked_provider_identities)
        work_identity = self.work_identity
        groups = tuple(replace(group, provider_identity=None) for group in groups)
        object.__setattr__(self, "presentation_groups", groups)
        object.__setattr__(self, "work_identity", work_identity)
        object.__setattr__(self, "linked_provider_identities", tuple(dict.fromkeys(links)))

    @classmethod
    def from_mapping(
        cls,
        *,
        task_id: str,
        source_root: Path,
        mapping: Mapping[Path, Path],
        expected_sources: Optional[Iterable[Path]] = None,
        ignored_sources: Iterable[Path] = (),
        metadata: Optional[Mapping[str, object]] = None,
        subtitle_contracts: Optional[Mapping[Path, SubtitlePlanContract]] = None,
        series_scope: Optional[SeriesScopeIdentity] = None,
        execution_policy: Optional[ExecutionPolicy] = None,
        presentation_groups: Iterable[PresentationGroup] = (),
        presentation_assets: Optional[
            Mapping[Path, PresentationAssetPlanContract]
        ] = None,
    ) -> "RenamePlan":
        contracts = {
            Path(source): contract
            for source, contract in (subtitle_contracts or {}).items()
        }
        entries = []
        assets = {
            Path(source): contract
            for source, contract in (presentation_assets or {}).items()
        }
        for source, target in mapping.items():
            entry = PlanEntry.from_paths(Path(source), Path(target))
            contract = contracts.get(Path(source))
            if contract is not None:
                entry = PlanEntry(
                    source_path=entry.source_path,
                    target_path=entry.target_path,
                    season_id=entry.season_id,
                    episode_id=entry.episode_id,
                    media_kind="subtitle",
                    role="subtitle",
                    requires_review=(
                        contract.association_state != DimensionResolution.RESOLVED
                        or contract.parse_state != DimensionResolution.RESOLVED
                        or bool(contract.unknown_tokens)
                    ),
                    evidence=contract.reason_codes,
                    subtitle=contract,
                    presentation_asset=None,
                )
            elif Path(source) in assets:
                entry = PlanEntry(
                    source_path=entry.source_path,
                    target_path=entry.target_path,
                    season_id=entry.season_id,
                    episode_id=entry.episode_id,
                    media_kind=entry.media_kind,
                    role=entry.role,
                    requires_review=entry.requires_review,
                    evidence=entry.evidence,
                    subtitle=None,
                    presentation_asset=assets[Path(source)],
                )
            entries.append(entry)
        entries = tuple(entries)
        expected = (
            tuple(Path(path) for path in expected_sources)
            if expected_sources is not None
            else tuple(entry.source_path for entry in entries)
        )
        return cls(
            task_id=str(task_id),
            source_root=Path(source_root),
            entries=entries,
            expected_sources=expected,
            ignored_sources=tuple(Path(path) for path in ignored_sources),
            metadata=dict(metadata or {}),
            series_scope=series_scope,
            execution_policy=execution_policy,
            presentation_groups=tuple(presentation_groups),
        )

    def as_mapping(self) -> Dict[Path, Path]:
        """Return a fresh mapping for the legacy executor boundary."""

        return {
            entry.source_path: entry.target_path
            for entry in self.entries
        }

    def to_dict(self) -> Dict[str, object]:
        """Return the versioned JSON-compatible persistence contract."""

        return {
            "schema_version": PLAN_SCHEMA_VERSION,
            "task_id": self.task_id,
            "source_root": str(self.source_root),
            "entries": [entry.to_dict() for entry in self.entries],
            "expected_sources": [str(path) for path in self.expected_sources],
            "ignored_sources": [str(path) for path in self.ignored_sources],
            "metadata": _deep_thaw(self.metadata),
            "execution_policy": (
                self.execution_policy.to_dict() if self.execution_policy else None
            ),
            "series_scope": self.series_scope.to_dict() if self.series_scope else None,
            "subtitle_override_rules": [rule.to_dict() for rule in self.subtitle_override_rules],
            "revision_parent_fingerprint": self.revision_parent_fingerprint,
            "revision_action": self.revision_action,
            "revision_reason": self.revision_reason,
            "presentation_groups": [
                group.to_dict() for group in self.presentation_groups
            ],
            "work_identity": (
                self.work_identity.to_dict() if self.work_identity else None
            ),
            "linked_provider_identities": [
                identity.to_dict() for identity in self.linked_provider_identities
            ],
            "projection_receipt": (
                self.projection_receipt.to_dict()
                if self.projection_receipt is not None else None
            ),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "RenamePlan":
        version = data.get("schema_version")
        if version != PLAN_SCHEMA_VERSION:
            raise ValueError(
                "Unsupported plan schema_version: "
                f"{version!r}; expected {PLAN_SCHEMA_VERSION}"
            )
        try:
            task_id = data["task_id"]
            source_root = data["source_root"]
            raw_entries = data["entries"]
        except KeyError as exc:
            raise ValueError(f"Plan missing field: {exc.args[0]}") from exc
        if not isinstance(task_id, str) or not isinstance(source_root, str):
            raise ValueError("Plan task_id and source_root must be strings")
        if not isinstance(raw_entries, list):
            raise ValueError("Plan entries must be a list")
        if not all(isinstance(entry, Mapping) for entry in raw_entries):
            raise ValueError("Every Plan entry must be an object")

        expected_sources = _path_list(
            data.get("expected_sources", ()), "expected_sources"
        )
        ignored_sources = _path_list(
            data.get("ignored_sources", ()), "ignored_sources"
        )
        metadata = data.get("metadata", {})
        if not isinstance(metadata, Mapping):
            raise ValueError("Plan metadata must be an object")
        series_scope = data.get("series_scope")
        execution_policy = data.get("execution_policy")
        rules = data.get("subtitle_override_rules", ())
        groups = data.get("presentation_groups", ())
        work_identity = data.get("work_identity")
        linked_identities = data.get("linked_provider_identities", ())
        projection_receipt = data.get("projection_receipt")
        if series_scope is not None and not isinstance(series_scope, Mapping):
            raise ValueError("Plan series_scope must be object or null")
        if execution_policy is not None and not isinstance(execution_policy, Mapping):
            raise ValueError("Plan execution_policy must be object or null")
        if not isinstance(rules, (list, tuple)) or not all(isinstance(item, Mapping) for item in rules):
            raise ValueError("Plan subtitle_override_rules must be objects")
        if not isinstance(groups, (list, tuple)) or not all(
            isinstance(item, Mapping) for item in groups
        ):
            raise ValueError("Plan presentation_groups must be objects")
        if work_identity is not None and not isinstance(work_identity, Mapping):
            raise ValueError("Plan work_identity must be object or null")
        if not isinstance(linked_identities, (list, tuple)) or not all(
            isinstance(item, Mapping) for item in linked_identities
        ):
            raise ValueError("Plan linked_provider_identities must be objects")
        revision_fields = {}
        for name in (
            "revision_parent_fingerprint", "revision_action", "revision_reason"
        ):
            value = data.get(name, "")
            if not isinstance(value, str):
                raise ValueError(f"Plan {name} must be a string")
            revision_fields[name] = value
        return cls(
            task_id=task_id,
            source_root=Path(source_root),
            entries=tuple(PlanEntry.from_dict(entry) for entry in raw_entries),
            expected_sources=expected_sources,
            ignored_sources=ignored_sources,
            metadata=dict(metadata),
            execution_policy=(
                ExecutionPolicy.from_dict(execution_policy)
                if isinstance(execution_policy, Mapping) else None
            ),
            series_scope=SeriesScopeIdentity.from_dict(series_scope) if isinstance(series_scope, Mapping) else None,
            subtitle_override_rules=tuple(SubtitleOverrideRule.from_dict(item) for item in rules),
            presentation_groups=tuple(PresentationGroup.from_dict(item) for item in groups),
            work_identity=(
                WorkIdentity.from_dict(work_identity)
                if isinstance(work_identity, Mapping) else None
            ),
            linked_provider_identities=tuple(
                ProviderIdentity.from_dict(item) for item in linked_identities
            ),
            projection_receipt=(
                ProjectionReceipt.from_dict(projection_receipt)
                if isinstance(projection_receipt, Mapping) else None
            ),
            **revision_fields,
        )


def _target_identity(target_path: Path) -> Tuple[Optional[int], Optional[int]]:
    season_id: Optional[int] = None
    episode_id: Optional[int] = None

    for part in target_path.parts:
        match = _SEASON_DIRECTORY.fullmatch(part)
        if match:
            season_id = int(match.group("season"))

    token = _EPISODE_TOKEN.search(target_path.name)
    if token:
        season_id = int(token.group("season"))
        episode_id = int(token.group("episode"))

    return season_id, episode_id


def _optional_int(value: object, field_name: str) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"Plan entry {field_name} must be an integer or null")
    return value


def _path_list(value: object, field_name: str) -> Tuple[Path, ...]:
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(item, str) for item in value
    ):
        raise ValueError(f"Plan {field_name} must be a list of strings")
    return tuple(Path(item) for item in value)


def _string_tuple(value: object, field_name: str) -> Tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(item, str) for item in value
    ):
        raise ValueError(f"Plan {field_name} must be a list of strings")
    return tuple(value)


@dataclass(frozen=True, eq=False)
class _FrozenMapping(Mapping):
    _data: Mapping

    def __post_init__(self):
        object.__setattr__(self, '_data', MappingProxyType(
            {str(key): _deep_freeze(item) for key, item in self._data.items()}))

    def __getitem__(self, key):
        return self._data[key]

    def __iter__(self):
        return iter(self._data)

    def __len__(self):
        return len(self._data)


def _deep_freeze(value: object) -> object:
    if type(value) is _FrozenMapping:
        return value
    if isinstance(value, Mapping):
        return _FrozenMapping(value)
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(item) for item in value)
    return value


def _deep_freeze_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    return _deep_freeze(value)


def _deep_thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _deep_thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_deep_thaw(item) for item in value]
    return value


def is_movie_plan(plan: RenamePlan) -> bool:
    return {group.kind for group in plan.presentation_groups} == {"movie_continuous"}
